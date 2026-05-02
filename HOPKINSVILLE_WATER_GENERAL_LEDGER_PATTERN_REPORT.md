# Hopkinsville Water — General Ledger Pattern Report

**Date:** 2026-05-02
**Source:** `Gl Codes/General Ledger Report.csv`
**Method:** Substring scan for `"hopkinsville water"` and standalone `" hwea "` across every column of every row. The report file was opened read-only — no rows added, removed, or modified.

---

## Summary

| Metric | Value |
| --- | --- |
| GL rows inspected | 27,143 (entire General Ledger Report) |
| Rows attributed to Hopkinsville Water | **1,432** |
| Distinct vendor strings observed | 2 (`Hopkinsville Water Environment Authority` × 1,432, `Aspen Meadows Apartments` × 5 — Hopkinsville-related credit/payment lines that mention HWEA elsewhere in the row) |
| Distinct properties on Hopkinsville rows | **4** — `AMA`, `GGOG`, `LLA`, `OTF` |
| Distinct account numbers parsed | **80** (regex `\d{4}-\d{5}-\d{3}` against Reference + Description) |
| Account → property attribution at 100 % confidence | **78 / 80** |
| Account → property attribution with only 2 supporting rows | 2 (flagged `needs_review: true` in YAML) |
| City of Henderson rows accidentally included | 0 — the search terms are HWEA-specific |
| Other water vendors accidentally included | 0 |

Search terms used: `hopkinsville water`, ` hwea ` (whitespace-padded). Vendor List spelling that won the match: `Hopkinsville Water Environment Authority`.

---

## Vendor name & aliases

| Spelling | Source | Action |
| --- | --- | --- |
| `Hopkinsville Water Environment Authority` | GL Report Vendor column (1,432 rows) and `Vendors/Vendor List.csv` | **Use as canonical `vendor_identity.vendor_name`** |
| `Hopkinsville Water Env. Auth.` | YAML alias only (not observed in GL) | Keep as alias |
| `HWEA` | YAML alias + bill body text | Keep as alias |
| `Hopkinsville Water` | YAML alias only | Keep as alias |

---

## Properties observed

All 1,432 HWEA GL rows attribute to one of four properties:

| Property abbreviation | Property name (inferred) | Account count | GL row count |
| --- | --- | ---: | ---: |
| **AMA** | Aspen Meadow Apartments | 67 | 1,156 |
| **GGOG** | Griffin Gate Oak Grove (Aspen / Griffin Gate Dr units B/C-NN) | 10 | 181 |
| **LLA** | Lakes of Lakeshore Apartments / Phantom Holdings (Talbert Dr) | 1 | 44 |
| **OTF** | Oak Tree Forest (Oak Tree Villa Dr) | 2 | 51 |

These are the same four properties that show up in the existing `property_address_overrides` block in YAML (TALBERT → LLA, GRIFFIN GATE → GGOG, OAK TREE / PIN OAK → OTF, DENZIL → AMA).

---

## GL accounts observed

The GL Report has the GL line on the **Cash – Operating Account** for the bank-side of every HWEA payment. The expense-side GL codes for these utility charges live in the YAML `service_gl_mapping` block already (6955 / 6995 / 6940 / 6956). No new GL codes need to be added.

| GL_Account string observed | Side | Use |
| --- | --- | --- |
| `1120 Cash - Operating Account` | bank credit (the payment) | Out of scope for AP import |
| (expense GL codes 6955 / 6995 / 6940 / 6956) | YAML | Already encoded in `service_gl_mapping` |

---

## Description patterns observed

The Description column on bank-side rows follows the pattern `<address> - <service>` or `<address> Apt N - <service>`. Examples:

```
'2501 S Virginia St - Storm Water'
'2629 Kenwood Dr 2 - Water'
'1100 Denzil Dr Apt 6 - Sewer'
'304 Griffin Gate Dr B9 - State Tax'
'101 Talbert Dr 1 - Water Pilot'
```

Useful: the description carries enough information to backfill the historical service address per account. The processor's Phase 1H multi-strategy resolver uses these as Strategy 5 (`historical_service_address_from_account_mapping`) when the bill's address didn't match Unit Info Clean directly. They flow into the Invoice Description and Line Item Description rendering only — never into the Location column unless the unit also validates against Unit Info Clean.

---

## Invoice number examples

The Reference column on bank rows is usually a check number (e.g. `2437`), not a ResMan invoice number. The HWEA processor builds invoice numbers itself using the YAML format `{account_number} {Mon} {YY}` (e.g. `0031-24022-094 Apr 26`). This follows the convention used elsewhere in the project (Richmond uses the same shape) and matches what GL Report references show on the EXPENSE side of the entry when it's present.

---

## Account → Property attribution table (high-confidence subset)

A few representative entries (full 80 in `config/vendors/hopkinsville_water_environment_authority.yaml` `account_property_unit_mapping.mappings`):

| Account # | Property | Historical service address | GL evidence rows | Confidence |
| --- | --- | --- | ---: | --- |
| `0031-23129-099` | AMA | 2629 Kenwood DR apt 1 | 6 | high |
| `0031-23134-100` | AMA | 2629 Kenwood Dr 2 | 30 | high |
| `0031-23997-073` | AMA | 2501 S Virginia St Apt 5 | 4 | high |
| `0031-24022-094` | AMA | 10 S Virginia Street | 8 | high |
| `0031-24037-079` | AMA | 2501 S Virginia St Apt 14 | 4 | high |
| `0035-27247-042` | AMA | 600 Denzil Dr 1 | 28 | high |
| `0035-27867-063` | AMA | 1900 Denzil Dr 9 | 36 | high |
| `0035-27522-052` | AMA | 1300 Denzil Drive Apt 11 | 31 | high |
| `0036-28653-003` | LLA | 101 Talbert Dr 1 | 44 | high |
| `0060-49352-003` | OTF | 2101 Oak Tree Villa Dr | 24 | high |
| `0070-56024-003` | GGOG | 310 Griffin Gate Dr F1 | 21 | high |
| `0070-57609-003` | GGOG | 305 Griffin Gate Dr C1 | 21 | high |
| `0250-95632-003` | AMA | 2501 S Virginia St | 6 | high |

### Medium-confidence (needs_review: true)

| Account # | Property | Historical address | Evidence | Why review |
| --- | --- | --- | --- | --- |
| `0035-27227-101` | AMA | 500 Denzil drive Apt 1 | 2 rows | Only 2 supporting GL rows — recommend a human glance |
| `0035-27272-102` | AMA | 700 Denzil Drive Apt 2 | 2 rows | same |

---

## Conflicts / inconsistencies found

None — every one of the 80 accounts has a SINGLE property. No account ever appears across two different `Property` values in the GL.

The `Description` column has some artefacts from older imports — the address is sometimes truncated (e.g., `'10 S Virginia Street'` is almost certainly the tail of `'2501 S Virginia Street'` after a regex chop in the legacy script). The new processor does NOT use these truncated addresses for matching; it only uses them as a description-fallback string.

---

## City of Henderson exclusions

Confirmed: zero City of Henderson rows leaked into the Hopkinsville evidence set. The search terms `"hopkinsville water"` and `" hwea "` don't match any Henderson row, and the GL Report's Vendor column for Henderson rows says `"City of Henderson"` which our YAML's `reject_if_text_contains: ["City of Henderson"]` already filters out at the bill-detection stage.

---

## How the processor uses this evidence

The new YAML block:

```yaml
account_property_unit_mapping:
  enabled: true
  source: "general_ledger_history"
  generated_at: "2026-05-02"
  evidence_summary: |
    1432 General Ledger rows attributed to Hopkinsville Water Environment
    Authority. 80 distinct account numbers found, all mapped to one of
    four properties: AMA, GGOG, LLA, OTF.
  mappings:
    "<account>":
      property_abbreviation: "<AMA|GGOG|LLA|OTF>"
      historical_service_address: "<best evidence string>"
      confidence: "<high|medium>"
      gl_evidence_rows: <int>
      needs_review: <bool>
```

The processor consults this block as **strategy 1** of the property/unit resolver (after the bill itself but before legacy address overrides). The mapping never overrides explicit, validated bill data:

- `property_abbreviation` is taken straight from the GL evidence — high confidence is acceptable.
- `historical_service_address` is **never** written into Location (Location must validate against Unit Info Clean.csv) — it's only used to fill the description when the bill's address can't be matched.
- `medium` confidence (just 2 supporting GL rows) is still applied, but `needs_review: true` flags the entry so an operator can confirm.

---

## Recommended user review (optional)

The following accounts are mapped at high confidence by the GL but the historical address string is suspicious. They map correctly to property `AMA` and they don't break anything — but if an operator wants a clean handoff, they can edit the `historical_service_address` field in YAML.

| Account # | Historical address (suspect) | Likely correct |
| --- | --- | --- |
| `0031-24022-094` | `10 S Virginia Street` | `2501 S Virginia St` (the bill itself) |
| `0035-28057-093` | `26 Denzil Drive Apt` | `2600 Denzil Drive Apt N` (incomplete suffix in GL) |
| `0250-96014-003` | (no historical address — only property AMA) | unknown — operator confirms |
| `0250-96924-002` | (no historical address — only property GGOG) | unknown — operator confirms |

These are minor — they don't affect the Property Abbreviation populating. The `historical_service_address` field is consumed only by description rendering.

---

## Files generated

- [`config/vendors/hopkinsville_water_environment_authority.yaml`](config/vendors/hopkinsville_water_environment_authority.yaml) — `account_property_unit_mapping` block (80 entries).
- [`HOPKINSVILLE_WATER_GENERAL_LEDGER_PATTERN_REPORT.md`](HOPKINSVILLE_WATER_GENERAL_LEDGER_PATTERN_REPORT.md) — this report.
- [`HOPKINSVILLE_WATER_ADDRESS_UNIT_MATCH_FIX_REPORT.md`](HOPKINSVILLE_WATER_ADDRESS_UNIT_MATCH_FIX_REPORT.md) — pairs with this; explains how the evidence is consumed.

The General Ledger Report and Chart of Accounts and Vendor List were all opened **read-only**. No source data file was modified.
