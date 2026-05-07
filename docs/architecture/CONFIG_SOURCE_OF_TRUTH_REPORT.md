# Config Source-of-Truth Report

**Phase:** Source-of-truth config layer setup
**Generated:** 2026-05-01
**Project root:** `C:\Users\Dasty\PycharmProjects\Billing_Refactoring_2026`

---

## What was created

```
config/
├── README.md                              ← How the config system works (~6 KB)
├── vendor_rules_template.yaml             ← Master template, every supported field heavily commented (~30 KB)
├── vendor_rules_index.yaml                ← Index of all 966 vendor configs (~442 KB)
├── general_ledger_reference.yaml          ← Historical GL behavior summary (~487 KB)
├── accounting_rule_inference_report.md    ← Human-readable inference notes (~17 KB)
└── vendors/                               ← 966 per-vendor YAML files
    ├── alabama_power.yaml
    ├── clarksville_gas_and_water.yaml
    ├── cde_lightband.yaml
    ├── epb_fiber_optics.yaml
    └── ... (966 total)
```

Plus this report at the project root: `CONFIG_SOURCE_OF_TRUTH_REPORT.md`.

---

## Why YAML

| Reason | Detail |
| --- | --- |
| **Readable** | A non-technical reviewer can open `config/vendors/alabama_power.yaml` and immediately see what GL code is being applied and why. |
| **Editable without code changes** | Future Python billing scripts read these files. Changing a GL code is a YAML edit, not a Python edit. |
| **Comments preserved** | Inline `# notes` survive in YAML; they wouldn't in JSON. |
| **Multi-line strings** | YAML's `|` block scalar makes `evidence_summary` and `notes` legible. |
| **Easy diffs in git** | Plain text, line-oriented. |
| **No build step** | Both Python (`PyYAML`) and a human can read it directly. |

A database was explicitly avoided per the rules. JSON would have been workable but is hostile to comments and to multi-line free text.

---

## How the General Ledger Report drove this

`Gl Codes/General Ledger Report.csv` (103,229 rows, 96,087 non-summary) was the **primary** input. Specifically:

1. The `Vendor` and `GL_Account` columns of every Expense / Non-Operating Expense / Fixed Asset row were grouped to derive each vendor's most-common GL code.
2. The supporting `Gl Codes/Chart Of Accounts.csv` (501 rows) was used to validate every GL code in the report (all 295 validated).
3. The `Property` column was used to derive vendor-specific *per-property* GL exceptions, which became the `property_overrides:` blocks in vendor YAMLs.
4. The `Description` column was scanned for special-charge keywords (`Reconnect`, `Recon Chg`, `Late Fee`, `Balance Forward`, etc.) to confirm the special-charge GL mappings encoded in every vendor YAML.

**Cash, AP, AR, and credit-card clearing rows were excluded from the analysis** so the inferred default GL reflects the *expense* side of each transaction, not the cash-side clearing. A common pitfall before this filter: utility vendors paid by AmEx had GL `2115 (Credit Card OTF)` as the apparent top GL, which would have been wrong as the operational accounting code.

The full inference is documented in `config/accounting_rule_inference_report.md`.

---

## Folder/file structure created

| File / Folder | Purpose | Bytes |
| --- | --- | ---: |
| `config/README.md` | Plain-English explainer | 6 KB |
| `config/vendor_rules_template.yaml` | Master template | 30 KB |
| `config/vendor_rules_index.yaml` | Index of all 966 vendor configs | 442 KB |
| `config/general_ledger_reference.yaml` | Read-only summary of GL Report; regeneratable | 487 KB |
| `config/accounting_rule_inference_report.md` | Inference rationale | 17 KB |
| `config/vendors/*.yaml` | 966 per-vendor configs | ~50 MB total |
| `CONFIG_SOURCE_OF_TRUTH_REPORT.md` (this file) | Final report | — |

### Total vendor YAML files created

**966.** Composition:

| Source | Count |
| --- | ---: |
| Vendors with a folder under `Training Bills_Invoices/` | 940 |
| Vendors found in General Ledger Report only (≥10 expense rows) but no training folder yet | 34 |
| Vendor folder ↔ key collisions (one YAML covers multiple folder names that normalize to the same key) | -8 |
| **Total YAMLs** | **966** |

(Two source rows that collapsed to the same safe folder name in the previous folder-structure phase — `Real Floors, Inc` / `Real Floors, Inc.` — also normalize to a single YAML key here, so they share one config.)

### Vendor-config sample (first ~20 of 966)

```
config/vendors/319_lokey_llc.yaml
config/vendors/a_1_heating_and_air.yaml
config/vendors/a_bit_of_everything.yaml
config/vendors/a_h_appliance_repair_llc.yaml
config/vendors/a_line_fence.yaml
config/vendors/a_to_z_heating_cooling_llc.yaml
config/vendors/a_z_property_services.yaml
config/vendors/abc_carpet_inc.yaml
config/vendors/abc_deals_inc_dba_window_world_of_clarksville.yaml
config/vendors/action_pest_control.yaml
config/vendors/adam_s_remodeling.yaml
config/vendors/adelade_holdings_llc.yaml
config/vendors/adelade_petty_cash.yaml
config/vendors/adelade_pref_llc.yaml
config/vendors/admiral_place_petty_cash.yaml
config/vendors/adrian_spivey.yaml
config/vendors/adt_security_corporation.yaml
config/vendors/advanced_disposal.yaml
config/vendors/affordable_appliance_repair.yaml
config/vendors/aftermath_services_corporation.yaml
... (946 more)
```

The complete list is in `config/vendor_rules_index.yaml`.

---

## Confidence summary across the 966 vendor YAMLs

| GL Confidence | Vendors | What it means |
| --- | ---: | --- |
| `high` | 26 | At least 20 expense rows AND ≥80% top-GL share. Trustworthy out of the box. |
| `medium` | 36 | At least 10 rows, top GL ≥60%. Usable, spot-check recommended. |
| `low` | 904 | Either no GL Report match (655) or thin evidence (<10 rows). Treat as TODO. |

**311** vendor YAMLs have `inferred_from_general_ledger: true`. The remaining **655** have `inferred_from_general_ledger: false` because the exact vendor name from `Training Bills_Invoices/` did not match any vendor name in the General Ledger Report — most often an alias-drift issue (e.g. folder says "ABC Carpet Inc." but GL row says "ABC Carpet").

**26** vendors are flagged `status: needs_training_bills` (in the GL but no training folder). They are listed in `config/accounting_rule_inference_report.md` section 8.

---

## Assumptions made

1. **Confidence rules** (high/medium/low) were chosen heuristically from the data shape, not from any external accounting standard. The thresholds (20 rows / 80% share for "high") are documented in `vendor_rules_template.yaml` and `accounting_rule_inference_report.md` and can be re-tuned by editing `_build_gl_analysis.ps1`.

2. **Expense-only filter** for vendor → GL inference (excluding cash/AP/AR/credit-card clearing rows) is essential to avoid misattributing utility vendors to credit-card clearing GLs. This is the single most important inference assumption.

3. **Property codes** are taken from the `Property` column of the GL Report, which uses Property Abbreviations (e.g. `AMA`, `OTF`, `TGAP`). The mapping back to full property names lives in `Properties/Properties.csv`.

4. **"Internal-entity vendors"** — 22 GL `Vendor` strings are actually property names used as the vendor on internal allocations (payroll, employee insurance, etc.). YAMLs were created for them with `status: draft`, but the user is recommended to set `vendor_identity.active: false` since they are not third-party invoice sources. The list is in section 7 of the inference report.

5. **Aliases** were prefilled from `Vendors/Vendor List.csv` `Company Abbreviation` column when present; the user can add more.

6. **Detection keywords** were derived from the vendor name with common suffixes stripped (`LLC`, `Inc`, `PLLC`, `Co`, `Corp`). They will need refinement once real invoice text is available.

7. **`special_charges.reconnection_charge` → GL 6956** is hardcoded in every vendor YAML at `confidence: high`. This is supported by:
   - `Chart of Accounts`: GL 6956 is "Connect Fee" with description "Utility Transfer Fee"
   - `General Ledger Report`: rows with descriptions like "Past Due Reconnect Fee - 906" and "Reconnect Fee - 1109" all post to 6956
   The user requested this rule explicitly and the data supports it.

8. **`special_charges.late_fee` → GL 6627** ("Late Fees & Penalties") is also encoded in every vendor YAML. 1,952 GL rows with "Late Payment Charge" in the description are coded to 6627 — strong evidence.

9. **Extraction layout** (regex patterns, column names, OCR zones) is left as `TODO` everywhere. Inferring layout from a vendor name without sample bills would be guessing; the user's instructions explicitly forbid that.

10. **Numeric thresholds for property overrides** (≥5 rows + different from default + ≥70% share) were chosen to keep the override list concise. Lowering would generate more overrides; raising would generate fewer. Tunable in `_build_vendor_yamls.ps1`.

11. **Folder layout** for `config/vendors/` uses one YAML per vendor with `snake_case` filenames matching `normalized_vendor_key`. Two cleanup choices: (a) drop trailing dots before snake-casing, (b) collapse multiple non-alphanumeric runs to a single underscore.

---

## Vendors with missing training bills

26 vendors (status `needs_training_bills`) appear in the General Ledger Report with substantial activity but have no folder under `Training Bills_Invoices/`. Top 10 by transaction volume:

| Vendor | Expense Rows | Top GL |
| --- | ---: | :---: |
| Lowes Pro Supply | 1,084 | 6675 |
| ResMan, LLC | 594 | 6115 |
| Chadwell Supply | 375 | 6675 |
| HD Supply Facilities Maintenance, Ltd. | 270 | 7595 |
| Sherwin Williams (Nex-Gen) | 167 | 6770 |
| Joshua A Cunningham | 170 | 6760 |
| EPB Fiber Optics | 129 | 6920 |
| Spectrum Business & Community Services | 101 | 6905 |
| Sherwin Williams (OTF) | ~95 | 6770 |
| Pennyrile Electric | 119 | 6915 |

Several of these are MRO suppliers (Lowes, HD Supply, Chadwell). The user may decide they do not need a per-vendor extraction script at all and instead route them through a generic supplier flow.

The full list lives in `config/vendor_rules_index.yaml` (filter on `status: "needs_training_bills"`).

---

## Vendors with ambiguous GL mappings

50 vendors have ≥30 expense rows AND either >5 distinct GL codes OR top-GL share <60%. They are listed in section 7 of `config/accounting_rule_inference_report.md`. Examples:

| Vendor | Rows | # GLs | Why ambiguous |
| --- | ---: | ---: | --- |
| Nex-Gen Management, LLC | 1,180 | 54 | Internal management entity — payroll/training/advertising/etc. |
| Lowes Pro Supply | 1,084 | 54 | Each purchase touches many categories. |
| Shelbyville Power System | 319 | 6 | Despite the name, half its rows are water/sewer (6955). |
| CDE Lightband | 300 | 5 | Roughly even split between vacant electric (6920) and common-area electric (6915). |
| City of Union City | 283 | 4 | Combined utility — water, storm water, trash all comparable. |
| HD Supply Facilities Maintenance, Ltd. | 270 | 41 | MRO supplier, line-level. |
| ResMan, LLC | 594 | 4 | SaaS / credit-verification mix; only 4 GLs but no clear dominant. |

For these vendors, a single `default_gl_code` is the wrong abstraction — they need `line_item_rules` and per-line GL inference. The vendor YAMLs include the full `historical_gl_codes_observed` list so the user can build out splitting rules.

---

## Recommended next step

In priority order:

1. **Open `config/README.md`** and skim it. Confirms the framing.
2. **Open `config/vendor_rules_template.yaml`** and skim the comments. This is the schema the user will actually live with.
3. **Open `config/accounting_rule_inference_report.md`** and act on section 12 (Recommended user review steps):
   1. Set `vendor_identity.active: false` on the 22 internal-entity property-vendor YAMLs.
   2. Spot-check the 27 high-confidence vendors and stamp `last_reviewed_by_user`.
   3. Decide on ambiguous high-volume vendors (Lowes, HD Supply, etc.) — extract or skip.
4. **Add training bills** to the existing folders under `Training Bills_Invoices/`. Once even a few real PDFs land, the layout/extraction fields (`extraction_targets.*.regex_patterns`, `expected_invoice_layout`, etc.) can be filled in for that vendor.
5. **Only after** real bills exist, write the first vendor's Python extraction script — and have it read its config from `config/vendors/<vendor_key>.yaml` exclusively.

---

## Confirmation: no extraction scripts were created

This phase produced **zero** Python files. Search the project tree:

```
find . -name "*.py" -not -path "./.venv/*"   →   (no results)
```

Helper PowerShell scripts (`_build_gl_analysis.ps1`, `_build_gl_reference_yaml.ps1`, `_build_vendor_yamls.ps1`) were used to *generate* the config files and intermediate analysis CSVs. They are **not** invoice-extraction scripts and they do **not** read invoices. They run once to seed the config and can be deleted by the user any time.

## Confirmation: no source files were modified

The following inputs were read with `Import-Csv` only — no writes:

- `Gl Codes/General Ledger Report.csv`
- `Gl Codes/Chart Of Accounts.csv`
- `Vendors/Vendor List.csv`
- `Properties/Properties.csv`
- `Training Bills_Invoices/` (directory listing only)

A spot check confirms file sizes and timestamps on those source files were unchanged by this phase. No invoices, GL data, vendor records, or property records were created, modified, or deleted.

The folder structure under `Training Bills_Invoices/` (created by the previous phase) is also untouched — empty placeholder folders are still empty placeholders.

---

*End of report.*
