# Configuration: Vendor Source-of-Truth System

## Purpose

This `config/` folder is the **single editable source of truth** for how future Python billing scripts should behave. The intent is that you can change billing/extraction behavior by editing YAML files here — without ever editing Python.

If a Python script is making a vendor-specific decision (which GL code to use, how to find the property, how to handle a "Reconnect Fee", etc.), that decision must come from one of these YAML files. Python provides plumbing; YAML provides the rules.

## Folder layout

```
config/
├── README.md                              ← this file
├── vendor_rules_template.yaml             ← the master template, every supported field
├── vendor_rules_index.yaml                ← index of all vendor config files
├── general_ledger_reference.yaml          ← summary of historical GL behavior
├── accounting_rule_inference_report.md    ← what we inferred and our confidence
└── vendors/                               ← one YAML per vendor
    ├── alabama_power.yaml
    ├── clarksville_gas_and_water.yaml
    ├── epb_fiber_optics.yaml
    └── ...
```

## Primary accounting source

The single most important input that prefilled these configs is:

> **`Gl Codes/General Ledger Report.csv`**

It contains the actual transaction history (vendors, properties, GL codes, descriptions, amounts) for the 2026 year. Whenever you see a `most_common_gl_code` or `historical_gl_codes_observed` field in a vendor YAML, it came directly from rows in that report.

The supporting reference is:

> **`Gl Codes/Chart Of Accounts.csv`**

Used only to validate that a GL code exists in the chart of accounts. If a code is in the GL Report but missing from the chart, the YAML field `gl_validation_status` will say `not_found_in_chart_of_accounts` so you can investigate.

## How a Python script should use these files

A future Python script for, say, EPB invoices should:

1. Read `config/vendors/epb_fiber_optics.yaml`.
2. Take its `accounting_mapping.default_gl_code` as the GL code to use.
3. Take its `extraction_targets`, `line_item_rules`, `property_matching`, etc. as the rules for parsing the bill.
4. Apply any `property_overrides` if the invoice is for a specific property where the rules differ.
5. Never fall back to hardcoded vendor logic — if something is missing in YAML, the script should either skip or flag for manual review.

## Editing a rule

1. Open the vendor YAML, e.g. `config/vendors/alabama_power.yaml`.
2. Find the field, change the value.
3. Save.

That's it. Re-running the future script picks up the change.

## Disabling a rule without deleting it

Most fields with an `active` flag (special charges, validation rules, individual extraction targets, property overrides) accept `active: false`. Set that instead of removing the block — you keep the documentation in place and can re-enable it later.

For a whole vendor: set `vendor_identity.active: false` at the top of the vendor file. Future runs will skip that vendor.

## Documenting an assumption

Every section that infers from the GL Report has these companion fields:

- `confidence: high | medium | low`
- `evidence_summary: "<plain English notes>"`
- `needs_review: true | false`
- `notes: |` (free-form multi-line notes)

When you change a value manually, update `confidence` to `high` (you've verified it) and set `needs_review: false`. Add a line to the file's `change_log` at the bottom — that's how we keep an audit trail without a database.

## Adding a new vendor

1. Copy `vendor_rules_template.yaml` into `config/vendors/<snake_case_vendor_name>.yaml`.
2. Fill `vendor_identity` first (name, key, category, aliases, detection keywords).
3. Set `accounting_source.inferred_from_historical_transactions: false` (because you didn't find them in the GL Report).
4. Set `accounting_mapping.default_gl_code` to your best guess from the chart of accounts; mark `confidence: low` and `needs_review: true`.
5. Add an entry in `vendor_rules_index.yaml`.
6. (Optional) Create a training-bills folder under `Training Bills_Invoices/<Category>/<Vendor Name>/` and point `input_files.training_folder` at it.

## Vendor-level rules vs. property-specific overrides

Most rules live at the vendor level — they apply every time we see an invoice from that vendor.

Some rules differ by property. For example, the same water company might be coded to GL 6955 (Water & Sewer) for most properties but GL 6950 (Water Bill - Irrigation/Sprinklers) for one property's irrigation meter. That goes in the `property_overrides` block of that vendor's YAML, keyed by property code (`AMA`, `OTF`, `TGAP`, etc., as listed in `Properties/Properties.csv`).

The override only replaces the fields it explicitly names. Everything else falls back to the vendor-level defaults.

## Things Python scripts should never do

- **Never** hardcode a vendor's GL code in `.py`. It must come from the vendor YAML.
- **Never** hardcode a "Reconnect Fee = GL 6956" rule in `.py`. That belongs in `special_charges` in the vendor YAML.
- **Never** hardcode a property code lookup. Use `property_matching` rules from the vendor YAML.
- **Never** hardcode an invoice description format. Use `accounting_mapping.invoice_description_format` and `line_item_description_format`.
- **Never** silently override config. If the script disagrees with the YAML, it should fail loudly or write to a manual-review report.

## The change log convention

Each vendor YAML ends with a `change_log:` list. Append entries at the end (newest at the bottom) like:

```yaml
change_log:
  - date: 2026-05-01
    changed_by: system_inference
    change_summary: Initial draft inferred from General Ledger Report.
    reason: Initial setup of source-of-truth system.
  - date: 2026-05-15
    changed_by: jdoe
    change_summary: Switched default_gl_code from 6920 to 6915.
    reason: Verified with property manager — common-area meter, not vacant unit.
```

Keep entries short. The diff in git tells the rest of the story.
