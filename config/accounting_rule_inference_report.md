# Accounting Rule Inference Report

**Generated:** 2026-05-01
**Primary input:** `Gl Codes/General Ledger Report.csv`
**Supporting input:** `Gl Codes/Chart Of Accounts.csv`, `Properties/Properties.csv`, `Vendors/Vendor List.csv`

This report is the human-readable companion to `general_ledger_reference.yaml`. It explains *what* was inferred and at *what confidence*.

---

## 1. Source File Statistics

| Metric | Value |
| --- | ---: |
| Rows in General Ledger Report (incl. header) | 103,230 |
| Data rows | 103,229 |
| "Summary - DATE" rollup rows (one per property per day; excluded from analysis) | 7,142 |
| Non-summary rows (used for analysis) | **96,087** |
| Distinct vendor strings in any row | 3,601 |
| Distinct vendor strings in expense-type rows only | **339** |
| Distinct GL accounts seen in the report | **295** |
| Distinct property codes seen in the report | **36** |
| Date range observed | 2026-01-01 → 2026-05-09 |
| Rows in Chart of Accounts | 501 |

### Columns detected in General Ledger Report

```
GL_Account, Date, Reference, Property, Vendor, Description,
Debit, Credit, Balance, Net Amount, Gl Accounts.Type, Mode,
Month Name, Year
```

### Filter applied for "expense-side" analysis

When deriving each vendor's `most_common_gl_code`, only rows where `Gl Accounts.Type` is one of:

- `Expense`
- `Non-Operating Expense`
- `Fixed Asset`

…are counted. Rows in `Bank`, `Accounts Payable`, `Accounts Receivable`, `Other Current Asset`, `Other Current Liability`, and `Equity` are excluded so that the inference reflects the **expense side** of each transaction (where the vendor billing rule is applied), not the cash/credit-card-clearing side.

Without this filter, vendors paid by credit card would have their primary GL code shown as the credit card clearing account (e.g. 2115 "Credit Card OTF") instead of the actual expense GL.

---

## 2. GL Code Validation Against Chart of Accounts

| Validation Status | Count |
| --- | ---: |
| `validated` (code present in Chart of Accounts) | **295** |
| `not_found_in_chart_of_accounts` | **0** |

Every GL code that appears in the General Ledger Report is also defined in `Gl Codes/Chart Of Accounts.csv`. No orphan codes were found.

---

## 3. Vendor → GL Confidence Distribution

Confidence buckets are computed per vendor based on volume and consistency:

- **high** — at least 20 expense rows AND the most-common GL code accounts for at least 80% of them
- **medium** — at least 10 rows AND the top GL accounts for at least 60% (but doesn't qualify for high)
- **low** — everything else

| Bucket | Count |
| --- | ---: |
| High | **27** |
| Medium | **41** |
| Low | **271** |
| **Total** vendors with expense rows | **339** |

The 271 in "low" are mostly long-tail one-off vendors with under 10 transactions in 2026 — too thin to draw a stable mapping from. They are still given a per-vendor YAML, but with `needs_review: true` and a `low` confidence label.

---

## 4. High-Confidence Vendor → GL Mappings (27)

These mappings are trustworthy out of the box. The vendor YAMLs for these vendors have `confidence: high` and `needs_review: false` on the accounting block.

| Vendor | GL Code | GL Description | Top GL Share | Rows |
| --- | :---: | --- | ---: | ---: |
| Alabama Power | 6920 | Electric - Vacant | 85.4% | 355 |
| Epremium Insurance Agency, LLC | 6171 | Renters Insurance Cost | 100% | 207 |
| Sherwin Williams (Nex-Gen) | 6770 | Paint & Supplies | 87.4% | 167 |
| Clarksville Gas and Water | 6955 | Water & Sewer | 83.8% | 117 |
| The Law office of Jennifer Mccoy | 6205 | Attorney fees & Eviction Costs | 88.4% | 112 |
| Apartments.com | 6335 | Media Advertising | 100% | 101 |
| Richmond Utilities | 6955 | Water & Sewer | 87.8% | 82 |
| Nolin RECC Smarthub | 6920 | Electric - Vacant | 86.9% | 61 |
| Hardin County Water District No. 2 | 6955 | Water & Sewer | 100% | 56 |
| Tennessee American Water | 6955 | Water & Sewer | 96.4% | 55 |
| Zillow Rentals | 6335 | Media Advertising | 100% | 48 |
| B & K Plumbing LLC | 6565 | Plumbing - Contract | 90.7% | 43 |
| Michaela McClendon | 6750 | Contract Cleaning | 90.2% | 41 |
| JMH IT Innovations | 6315 | Domain / Website Expenses | 89.5% | 38 |
| Hall & Associates | 6205 | Attorney fees & Eviction Costs | 100% | 38 |
| Granite Telecommunications, LLC | 6178 | Telephone | 100% | 37 |
| Lookout Pest Control Formerly Ace Exterminating | 6560 | Pest Control - Contract | 100% | 32 |
| City of Chattanooga Wastewater Department | 6955 | Water & Sewer | 100% | 30 |
| Waste Solution Services | 6940 | Trash Collection / Removal | 100% | 28 |
| Owens Carpet Cleaning | 6740 | Contract Carpet Cleaning | 80.8% | 26 |
| Skips Mobil Maintenance LLC | 6530 | Contract Maintenance-Temp | 88% | 25 |
| Kalixta Kleaning LLC | 6750 | Contract Cleaning | 100% | 24 |
| Republic Services, Inc. | 6940 | Trash Collection / Removal | 100% | 24 |
| Andrea Leon | 6750 | Contract Cleaning | 95.7% | 23 |
| James Knight Appliance Service | 6505 | Appliance - Contract | 95.5% | 22 |
| Waste Connections | 6940 | Trash Collection / Removal | 100% | 22 |
| Redd's Heating & Air Conditioning LLC | 6555 | HVAC - Contract | 90.5% | 21 |

### Notable utility-vendor patterns

- **Electric utilities** consistently land on **6920 (Electric - Vacant)** as the dominant GL because most of the year a property has some vacant units that don't pass through to a tenant. Common-area electric (6915) and billable electric (6910) split the rest.
- **Water/sewer utilities** consistently land on **6955 (Water & Sewer)**. Irrigation lines occasionally split off to **6950 (Water Bill - Irrigation/Sprinklers)** — see property overrides in section 6.
- **Trash/waste vendors** (Republic, Waste Connections, Waste Solution Services) all post to **6940 (Trash Collection / Removal)** at 100% share.
- **Pest control** vendors post to **6560 (Pest Control - Contract)**.
- **Cleaning** services post to **6750 (Contract Cleaning)**; carpet cleaning specifically to **6740**.

---

## 5. Medium-Confidence Vendor → GL Mappings (41)

Vendors with ≥10 expense rows where the top GL accounts for 60–80% of activity. These mappings are usable but should be spot-checked. Listed below in order of evidence weight.

(See `general_ledger_reference.yaml` `vendor_gl_patterns` section for the complete data.)

A representative sample:

| Vendor | Top GL | Top GL Share | Rows | Comment |
| --- | :---: | ---: | ---: | --- |
| Hopkinsville Water Environment Authority | 6955 | 76.3% | 658 | Mixed water/sewer/connection charges. |
| Hopkinsville Electric System | 6920 | 67% | 197 | Vacant + common area split; some 6915 / 6910. |
| Pennyrile Electric | 6915 | 68.1% | 119 | Common-area meter usage leans 6915. |
| EPB Fiber Optics | 6920 | 60.5% | 129 | Treated as electric in the GL (vacant unit electric); confirm whether this should re-map to 6960 Internet Service. |
| Top Notch Cleaning LLC | 6750 | 56.3% | 119 | Some 6740 (carpet cleaning) entries present. |
| Spectrum Business & Community Services | 6905 | 42.6% | 101 | Mixed cable / phone / internet — would benefit from line-level rules. |
| Cunningham Home Improvement LLC | 6760 | 65.7% | 67 | Painting contract dominates; some other repair codes. |

---

## 6. Vendor × Property Specific Patterns (basis for `property_overrides`)

When a vendor's GL pattern at a single property differs from its overall pattern, the per-vendor YAML gets a `property_overrides:` entry. The rule used to seed an override:

> The combination has at least 5 rows, AND its most-common GL is different from the vendor's overall most-common GL, AND that property-specific GL has at least 70% share of rows for that property.

Example overrides automatically seeded:

| Vendor | Property | Override GL | Share | Rows |
| --- | :---: | :---: | ---: | ---: |
| All American Cleaning and Restoration Inc | AMA | 6580 | 100% | 7 |
| Career Strategies, Inc. AZ | GGOG | 6530 | 100% | 5 |
| CDE Lightband | TEC | 6915 | 87.3% | 55 |
| Chadwell Supply | TEC | 7595 | 83.3% | 12 |
| Lowes Pro Supply | UGC | 7595 | 92.5% | 53 |
| Mike Myers | SWTG | 6565 | 80% | 5 |

These appear inside the `property_overrides:` block of the corresponding vendor's YAML in `config/vendors/`.

---

## 7. Ambiguous Mappings That Require Human Review (50 vendors with ≥30 rows)

These vendors have either many distinct GL codes (>5) or a top-GL share under 60% even with substantial transaction volume. They almost certainly represent **multi-line vendor invoices** that would benefit from `line_item_rules` rather than a single default GL code.

The top 10 by volume:

| Vendor | Rows | # GLs | Top GL | Top GL Share | Why ambiguous |
| --- | ---: | ---: | :---: | ---: | --- |
| Nex-Gen Management, LLC | 1,180 | 54 | 6335 | 20.5% | Internal management entity — payroll, advertising, training, etc., all flow through it. Best modeled as a **non-extractable internal vendor**, not a third-party invoice source. |
| Lowes Pro Supply | 1,084 | 54 | 6675 | 12.7% | A single Lowes purchase touches dozens of categories (appliance parts, plumbing supplies, painting, etc.). Line-level GL inference required. |
| ResMan, LLC | 594 | 4 | 6115 | 49.3% | SaaS/credit-verification mix; only 4 distinct GLs but dominated by no single one. |
| Chadwell Supply | 375 | 38 | 6675 | 11.7% | Same MRO supplier pattern as Lowes; line-level rules needed. |
| Shelbyville Power System | 319 | 6 | 6955 | 49.2% | Despite the name "Power", invoices route to **water/sewer** (6955) almost as often as electric (6920/6915). Investigate whether SPS bills both utilities, or whether GL coding has been inconsistent. |
| CDE Lightband | 300 | 5 | 6920 | 46% | Even split between vacant electric (6920) and common-area electric (6915). Use property/unit-level classification. |
| City of Union City | 283 | 4 | 6955 | 49.8% | Combined utility billing — water (6955), storm water (6995), and trash (6940) are roughly equal share. |
| HD Supply Facilities Maintenance, Ltd. | 270 | 41 | 7595 | 27% | MRO supplier, line-level. |
| Kros Home Services LLC | 199 | 19 | 7595 | 26.1% | General contractor with diverse work types. |
| The Adelade | 178 | 33 | 6470 | 13.5% | Property-as-vendor pattern — internal payroll/insurance allocation entries; treat as internal, not a third-party invoice source. |

A complete list lives in `general_ledger_reference.yaml` under `ambiguous_mappings`.

### Internal entity vendors

The General Ledger Report uses **property names** as vendor strings on internal allocation entries (e.g. "The Adelade", "The Glenwood at Pinson", "Aspen Meadows Apartments", "River Canyon", "Canoe Creek"). These show up in the vendor count but are not third-party invoice sources. Their YAMLs were created (status: `draft`) but should be marked **`active: false`** by the user before any extraction script runs.

Identified internal-entity vendor strings (this is the union of GL `Vendor` values that match a property name in `Properties.csv`):

```
The Adelade, Aspen Meadows Apartments, Blue Country Apartments,
Admiral Place Apartments, Canoe Creek, Griffin Gate Apartments -OG,
Harmony Square Townhomes, Liberty Landings, Magnolia Village Apartments,
Oak Tree Farms, River Canyon, Sage Flats, SW Gables Property, LLC,
The Element Clarksville, The Firefly, The Glenwood at Pinson,
The Oakley at Pro Park, The Park at Carson, The Penn Warren,
The Raintree Apartments, The Villas of Pine Valley,
Villages of Autumnwood
```

---

## 8. Vendors Found in GL Report Without a Training Folder

34 vendors appear in the General Ledger Report with ≥10 expense rows but do not yet have a folder under `Training Bills_Invoices/`. Their YAMLs are tagged `status: needs_training_bills` so they show up in `vendor_rules_index.yaml`. The user should:

1. Decide if a training folder is warranted for each.
2. Create `Training Bills_Invoices/<Category>/<Vendor>/` and drop sample bills in.
3. Update the `category` and `training_folder` fields in that vendor's YAML.

A representative subset (top 10 by row count):

```
Lowes Pro Supply
ResMan, LLC
Chadwell Supply
HD Supply Facilities Maintenance, Ltd.
Sherwin Williams (Nex-Gen)
Joshua A Cunningham
EPB Fiber Optics
Spectrum Business & Community Services
Sherwin Williams (OTF)
Pennyrile Electric
```

(Several of these — Lowes, Chadwell, HD Supply, Sherwin — are MRO/material vendors. The user may decide they don't need a per-bill extraction script at all and instead handle them via a generic supplier flow.)

---

## 9. Vendors Found in Training Bills Without a GL Report Match

655 vendor folders exist under `Training Bills_Invoices/` for which **no expense rows in the GL Report** matched the folder's vendor name. Reasons for this gap:

1. **Alias drift** — the GL Report uses a different spelling. (e.g. the folder is "ABC Carpet Inc." but the GL row says "ABC Carpet" without the "Inc.")
2. **Newly added vendor** — folder was created in advance of any 2026 transaction.
3. **One-off vendor** — vendor existed in pre-2026 history but has no current-year activity in this report.

These vendor YAMLs were generated with:

```yaml
accounting_source:
  inferred_from_historical_transactions: false
  most_common_gl_code: null
  confidence: low
  needs_review: true
accounting_mapping:
  default_gl_code: null
  gl_validation_status: not_inferred
```

**Recommended user review:** for each vendor that should have GL history, search `Vendors/Vendor List.csv` and the GL Report for alias matches and add them to the `aliases:` list inside the vendor YAML.

---

## 10. Description / Memo Patterns

The top 50 most-frequent description strings are listed in `general_ledger_reference.yaml` under `description_patterns`. They primarily reveal the patterns used by feeder systems:

- `"<Vendor> - <Account #> - Credit Adjustment"` — utility credit adjustments
- `"Past Due Reconnect Fee - <Unit>"` — reconnection charges (drives the 6956 mapping in special_charges)
- `"<Address> Apt <Unit> Connect fee balance forward"` — water company connect-fee balance forwards
- `"Reversed Payment <ID>: Trash Service Fee"` — tenant payment reversals (income side, not vendor side)

---

## 11. Special-Charge Keyword Scan

Performed across all expense rows for: `recon`, `reconnect`, `late`, `deposit`, `balance forward`, `service fee`, `credit`, `adjustment`. Detailed counts and sample descriptions are in `general_ledger_reference.yaml` under `special_charge_keyword_scan`. The most actionable findings:

| Keyword | Hits | Top GL | Note |
| --- | ---: | :---: | --- |
| `reconnect` | 7 | 6956 | Confirms `special_charges.reconnection_charge` → GL 6956 in vendor YAMLs. |
| `recon` (broader) | 9 | 6956 | Same. |
| `balance forward` | 26 | 6956 | Always tied to a connection-fee line; `subtract_from_current_charges` is the right default. |
| `late payment charge` | 1,952 hits on GL 6627 | 6627 | Confirms `special_charges.late_fee` → GL 6627. |
| `credit adjustment` | 56 | 1120/4250 | Mostly tenant-side; vendor-side credit handling stays at low confidence. |

The "Recon Chg → 6956" rule the user called out is **fully supported by the data** and is encoded in every vendor YAML's `special_charges.reconnection_charge` block.

---

## 12. Recommended User Review Steps

In priority order:

1. **Disable internal-entity "vendors"** — open the YAMLs for the 22 property-named entities listed in section 7 ("Internal entity vendors") and set `vendor_identity.active: false`. They are not real third-party invoice sources.

2. **Review the 27 high-confidence vendors** (section 4) — these are ready for production use. Set `accounting_source.last_reviewed_by_user: <today>` after a quick spot-check.

3. **Review the medium-confidence list** (section 5) — for each, look at the `historical_gl_codes_observed` block and either (a) confirm the inferred default, (b) override it, or (c) add a `line_item_rules` block to split the bill into multiple GL codes.

4. **Decide handling for ambiguous high-volume vendors** (section 7 — Lowes, HD Supply, Chadwell, Sherwin Williams, ResMan). These are best modeled with `split_by_unit: true` plus per-line GL inference, OR they can stay routed to manual review with no auto-extraction.

5. **Reconcile "no-GL-match" vendor folders** (section 9) — search the GL Report for alias matches and update the `aliases:` field in those YAMLs. Re-run the inference script after to see if the confidence improves.

6. **Decide which vendors in `needs_training_bills`** (section 8) actually need a training folder. Don't create folders for vendors you won't bother extracting.

---

## 13. What This Report Does NOT Tell You

- It cannot infer **invoice layout** (where on a PDF the account number appears, etc.). That requires real training bills.
- It cannot detect **OCR-friendliness** of the vendor's bills. Same — requires sample PDFs.
- It cannot predict **future GL changes**. If your accounting policy moves "Electric - Vacant" to a different code mid-year, the inference will go stale until the GL Report is re-run.
- It cannot identify **duplicate vendors under different names** with high confidence. That's the alias-drift problem in section 9.

---

*Generated by the source-of-truth-config setup phase. Re-running `_build_gl_analysis.ps1` followed by `_build_gl_reference_yaml.ps1` and `_build_vendor_yamls.ps1` regenerates this data set.*
