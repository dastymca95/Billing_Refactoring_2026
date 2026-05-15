# Webapp Phase U1 Utility Vendor Processor Expansion Report

Phase: U1 - Utility Vendor Deterministic Processor Expansion  
Workspace: `C:\Users\Dasty\PycharmProjects\Billing_Refactoring_2026`  
Date: 2026-05-13

## 1. Training Folder Discovery

The utility training inventory is documented in:

- `docs/reports/phases/UTILITY_VENDOR_DISCOVERY_REPORT.md`

Summary:

- 26 in-scope utility vendor YAMLs were found and annotated with U1 metadata.
- 8 vendors are already registered as active deterministic webapp processors.
- 5 vendors have old-script references and sufficient training data, but still need modern webapp processors.
- 13 vendors have training data and YAMLs but no active modern deterministic processor yet.

## 2. Old Scripts Analysis

The old script analysis is documented in:

- `docs/reports/phases/UTILITY_OLD_SCRIPTS_ANALYSIS_REPORT.md`

Key finding: old scripts contain useful parsing patterns, but also obsolete local paths and embedded Dropbox credential patterns. They were treated as reference only and were not copied into new code.

## 3. Shared Utility Framework

Added:

- `webapp/backend/services/utility_processor_common.py`
- `utils/utility_bill_parser.py`
- `utils/utility_tax_allocator.py`
- `utils/utility_invoice_number.py`
- `utils/utility_line_classifier.py`

The shared framework provides:

- common line classification for service/tax/payment/previous balance/connect fee/late fee
- proportional tax allocation with exact cents reconciliation
- utility invoice-number formatting
- utility invoice and line-item description composition
- default GL mapping by utility family
- Chart of Accounts validation
- required-field validation for utility ResMan rows
- raw-address-in-Location blocking
- standalone tax row blocking
- previous balance/payment expense row blocking

## 4. Canonical Utility Rules Added

Updated:

- `config/canonical_rules.yaml`
- `docs/CANONICAL_RULES_ENGINE.md`

The new `utility_processing:` canonical contract records:

- required utility template fields
- invoice-number default: `{account_number} {service_month_abbrev_title} {service_year_yy}`
- default `Bill`
- `Accounting Date = Invoice Date`
- Location must be Unit Info Clean valid only
- Property Abbreviation is required
- tax allocation is proportional and no standalone tax line is allowed
- connection/reconnection fee GL is `6956`
- late fee is never GL `6956`
- payment and previous balance rows are excluded by default

## 5. Vendors Implemented

These vendors were already implemented before U1 and now have U1 overlays and smoke coverage:

| Vendor key | Processor | Status |
| --- | --- | --- |
| `atmos_energy_auto_pay` | `process_atmos_energy_auto_pay.py` | active |
| `columbia_power_and_water_system` | `process_columbia_power_and_water_system.py` | active |
| `hardin_county_water_district_no_2` | `process_hardin_county_water_district_no_2.py` | active |
| `hopkinsville_water_environment_authority` | `process_hopkinsville_water_environment_authority.py` | active |
| `mcminnville_electric_system` | `process_mcminnville_electric_system.py` | active |
| `pennyrile_electric` | `process_pennyrile_electric.py` | active |
| `richmond_utilities` | `process_richmond_utilities.py` | active |
| `shelbyville_power_system` | `process_shelbyville_power_system.py` | active |

No existing vendor business logic was rewritten in this phase. The shared framework is ready for safe adoption by these processors in follow-up hardening passes.

## 6. Vendors Stubbed / Prepared

U1 overlays were added by `scripts/bootstrap_utility_vendor_configs.py` to:

- `alabama_power`
- `birmingham_water_works`
- `cde_lightband`
- `city_of_chattanooga_wastewater_department`
- `city_of_martin`
- `city_of_union_city`
- `city_of_mcminnville_water_sewer_dept`
- `clarksville_gas_and_water`
- `epb_fiber_optics`
- `guardian_water_power`
- `hopkinsville_electric_system`
- `kentucky_utilities`
- `knoxville_utilities_board`
- `nolin_recc_smarthub`
- `tennessee_american_water`
- `the_city_of_henderson`
- `union_city_energy_authority`
- `weakley_county_municipal_electric_system`

Status is explicitly marked as `needs_processor`, `needs_more_training`, or `partial_reference_old_script`; these are not claimed as complete.

## 7. Community / Master Billing Rules

The U1 overlay includes explicit community billing metadata:

- Kentucky Utilities: `community_billing_rules.enabled = true`
- Knoxville Utilities Board: `community_billing_rules.enabled = true`

The rule shape is:

- `master_invoice_strategy: one_per_master_account`
- `line_item_numbering: sequential_per_master_invoice`

This prevents accidentally applying master-account behavior to every vendor.

## 8. Tax Allocation Strategy

The shared rule is:

1. Identify tax-only rows.
2. Exclude tax-only rows from final ResMan output.
3. Allocate tax proportionally across positive, taxable, exportable service lines.
4. Round each line to cents.
5. Apply any rounding remainder to the largest taxable base line.
6. Keep allocation metadata for trace/debug.

This is intended to prevent tax-only rows in Penny Reel/Pennyrile, HWEA, and future utility processors.

## 9. Fee Handling Strategy

- Connection / reconnection / utility transfer fee: separate line, GL `6956`.
- Late fee: same underlying utility category or vendor default; never GL `6956`.
- Balance forward: include only when vendor YAML explicitly says it is current payable.
- Payments/autopay/amount enclosed: never exported as expense lines.
- Zero amount lines: excluded by default and traceable as notes if needed.

## 10. Existing Processor Fixes

Direct vendor logic was not rewritten, which avoids breaking Richmond Utilities, HWEA, Pennyrile, Shelbyville, McMinnville, Atmos, Columbia, and Hardin.

Framework-level fix applied:

- `Late payment charge` is now classified as `late_fee`, not as a payment row.

Documented follow-up:

- HWEA dry-run returns a workbook path even when the file is not written. The U1 smoke treats that as a warning if no workbook exists.
- Existing processors should be migrated gradually to call `utility_processor_common.py` after each parser extracts candidate lines.

## 11. Tests Performed

Commands run during implementation:

```powershell
python scripts\bootstrap_utility_vendor_configs.py
python scripts\smoke_utility_processors.py --contract-only
python scripts\smoke_utility_processors.py
python -m py_compile webapp\backend\services\utility_processor_common.py scripts\smoke_utility_processors.py scripts\bootstrap_utility_vendor_configs.py
```

`python scripts\smoke_utility_processors.py` passed. It validated 26 utility vendor YAML overlays and executed one-file temp-folder dry-runs for active utility processors. Dropbox was skipped by `dry_run`. One warning remains: HWEA returns a workbook path in dry-run even though the workbook file is not written.

Additional validation commands required before closing the full phase:

```powershell
python -m compileall webapp\backend
python scripts\verify_backend_routes.py
python scripts\smoke_canonical_rules_engine.py
python scripts\smoke_canonical_invoice_fixtures.py
cd webapp\frontend
npm.cmd run build
npx.cmd tsc --noEmit
npm.cmd run test:e2e
```

Final validation result:

- `python -m compileall webapp\backend` - PASS
- `python scripts\verify_backend_routes.py` - PASS
- `python scripts\smoke_canonical_rules_engine.py` - PASS
- `python scripts\smoke_canonical_invoice_fixtures.py` - PASS (`servall_pest` remains explicitly skipped for missing reliable source data)
- `python scripts\smoke_utility_processors.py` - PASS with one HWEA dry-run path warning
- `npm.cmd run build` - PASS
- `npx.cmd tsc --noEmit` - PASS
- `npm.cmd run test:e2e` - PASS, 22 passed / 2 skipped

## 12. Vendors Needing More Work

High priority next implementations:

- Alabama Power
- Clarksville Gas and Water
- EPB Fiber Optics
- Kentucky Utilities
- Knoxville Utilities Board
- Columbia/CPWS hardening against shared tax rules
- Tennessee American Water
- The City of Henderson
- Union City Energy Authority

Remaining but prepared:

- Birmingham Water Works
- City of Chattanooga Wastewater Department
- City of Martin
- City of Union City
- City of McMinnville Water & Sewer Dept
- Guardian Water & Power
- Hopkinsville Electric System
- Nolin RECC Smarthub
- Weakley County Municipal Electric System

## 13. Integrity

- `Output/Template.xlsx` was not modified.
- Source training bills were not modified.
- Old scripts were not modified.
- `.env` was not modified.
- Dropbox was not called during U1 smoke work.
- No AI provider calls were required for deterministic utility expansion.

## 14. Next Recommended Phase

Phase U2 should implement the Wave 2 processors using the shared framework:

1. Alabama Power
2. Clarksville Gas and Water
3. EPB Fiber Optics
4. Kentucky Utilities
5. Knoxville Utilities Board
6. The City of Henderson
7. Union City Energy Authority

Each U2 processor should include a one-file dry-run fixture, mandatory-field validation, tax allocation validation, property/location validation, and a Bulk/Single Invoice preview check.
