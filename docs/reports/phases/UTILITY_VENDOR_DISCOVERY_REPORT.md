# Utility Vendor Discovery Report

Phase: U1 - Utility Vendor Deterministic Processor Expansion  
Workspace: `C:\Users\Dasty\PycharmProjects\Billing_Refactoring_2026`  
Date: 2026-05-13  
Last updated: 2026-05-14 for Phase U3

## Scope

Scanned `Training Bills_Invoices/` for utility-related vendors under electricity/power, gas, water/sewer, internet/fiber, and community utility billing categories. Also checked current vendor YAML files, modern webapp processor registration, vendor-local processor scripts, processed-output examples, and matching old scripts.

No source training documents were modified.

## Vendor Inventory

| Vendor | Category | Training files | Old script found | Existing processor | Sample output found | Priority | Notes |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| Alabama Power | Electricity - Power | 60 PDF, 3 spreadsheets | `Alabama_Power.py` | No | No confirmed output | Wave 2 | Strong old-script reference; needs modern deterministic processor. |
| Atmos Energy Auto Pay | Gas | 10 PDF | No | Active | Yes | Wave 1 | Existing webapp processor; U1 overlay added. |
| Birmingham Water Works | Water - Sewer | 15 PDF | No | Active | No | Wave 3 | U3 deterministic parser active; splits water/sewer lines and reconciles current charges. |
| CDE Lightband | Electricity - Power | 81 PDF, 2 spreadsheets | `CDE Light Band.py` | No | No confirmed output | Wave 2 | User spelling note resolved: actual folder is `CDE Lightband`. |
| City of Chattanooga Wastewater Department | Water - Sewer | 18 PDF | No | Active | No | Wave 3 | Distinct from generic City of Chattanooga; U3 parser active for sewer usage statements. |
| City of Martin | Water - Sewer | 6 PDF | No | Active | No | Wave 3 | U3 municipal statement parser active; ignores payment history and uses current charges. |
| City of McMinnville Water & Sewer Dept | Water - Sewer | 6 PDF | No | Active | No | Wave 3 | Separate from McMinnville Electric; U3 parser active for water/sewer/garbage/tax current charges. |
| City of Union City | Water - Sewer | 31 PDF | No | Active | No | Wave 3 | U3 parser active for water/sewer/sanitation/stormwater lines. |
| Clarksville Gas and Water | Water - Sewer | 25 PDF | No | Active | No | Wave 3 | U3 parser active for scanned OCR bills; allocates sales tax across water/sewer. |
| Columbia Power and Water System | Electricity - Power | 5 PDF, processed outputs present | `CPWS.py` | Active | Yes | Wave 1 | Existing processor registered. |
| EPB Fiber Optics | Electricity - Power | 23 PDF, 2 spreadsheets | `EPB_Fiber.py` | No | No confirmed output | Wave 2 | Fiber/internet utility; use GL 6960 unless rules override. |
| Guardian Water & Power | Water - Sewer | 5 PDF | No | Active | No | Wave 3 | U3 parser active; captures active/vacant/final billing-fee rows. |
| Hardin County Water District No. 2 | Water - Sewer | 10 PDF, processed outputs present | `Hardin CWD2.py` | Active | Yes | Wave 1 | Existing processor registered. |
| Hopkinsville Electric System | Electricity - Power | 25 PDF | No | Active | No | Wave 3 | U3 parser active; separates connect fee to GL 6956 and allocates taxes into service. |
| Hopkinsville Water Environment Authority | Water - Sewer | 441 PDF, 24 CSV, 48 spreadsheets | `HWEA Test.py` | Active | Yes | Wave 1 | Existing processor registered; heavy training library. |
| Kentucky Utilities | Electricity - Power | 9 PDF | No | Active | No | Wave 3 | U3 parser active; explicit community billing flag remains vendor-scoped. |
| Knoxville Utilities Board | Electricity - Power | 8 PDF | No | Active | No | Wave 3 | U3 parser active for summary-by-address community billing; line numbering stays sequential per master invoice. |
| McMinnville Electric System | Electricity - Power | 5 PDF, processed outputs present | No | Active | Yes | Wave 1 | Existing processor registered. |
| Nolin RECC Smarthub | Electricity - Power | 6 PDF, 2 spreadsheets | `Nolin REC.py` | No | No confirmed output | Wave 2 | Folder spelling verified as `Nolin RECC Smarthub`. |
| Pennyrile Electric | Electricity - Power | 34 PDF, processed outputs present | `Pennyrile Bills.py` | Active | Yes | Wave 1 | Existing processor registered. |
| Reach Municipalities / Richmond Utilities | Water - Sewer | 15 PDF, 50 CSV, 72 spreadsheets | No | Active | Yes | Wave 1 | User phrase maps to existing `Richmond Utilities`. |
| Shelbyville Power System | Electricity - Power | 15 PDF, processed outputs present | `Shelbyville Power.py` | Active | Yes | Wave 1 | Existing processor registered. |
| Tennessee American Water | Water - Sewer | 24 PDF | No | Active | No | Wave 3 | U3 parser active; handles service-related charges, taxes, and negative fees/adjustments. |
| The City of Henderson | Electricity - Power | 66 PDF, 1 spreadsheet | `Henderson Bills.py` | No | No confirmed output | Wave 2 | Actual vendor YAML key is `the_city_of_henderson`. |
| Union City Energy Authority | Electricity - Power | 24 PDF | No | Active | No | Wave 3 | U3 OCR parser active; due date is read from detached payment coupon text. |
| Weakley County Municipal Electric System | Electricity - Power | 17 PDF, 7 images | No | Active | No | Wave 3 | U3 parser active for scanned/OCR electric bills; image samples remain supported by ingestion fallback. |

## Existing Active Utility Processors

These are already registered in `webapp/backend/services/batch_processor.py`:

- `atmos_energy_auto_pay`
- `columbia_power_and_water_system`
- `hardin_county_water_district_no_2`
- `hopkinsville_water_environment_authority`
- `mcminnville_electric_system`
- `pennyrile_electric`
- `richmond_utilities`
- `shelbyville_power_system`
- `alabama_power`
- `birmingham_water_works`
- `cde_lightband`
- `city_of_chattanooga_wastewater_department`
- `city_of_martin`
- `city_of_mcminnville_water_sewer_dept`
- `city_of_union_city`
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

## YAML Overlay Status

All 26 in-scope utility vendors now have a `utility_processing:` overlay in `config/vendors/<vendor>.yaml`. The overlay records:

- U1 status: `active`, `partial_reference_old_script`, `needs_processor`, or `needs_more_training`
- training folder and counts
- old script reference if one exists
- current processor if registered
- deterministic processing mode
- canonical tax/fee/property/location validation contract
- community billing settings where applicable

## Important Discovery Notes

- No modern deterministic processor is claimed complete unless it is registered and already callable through the webapp batch processor.
- Old scripts are useful but contain obsolete local paths and embedded Dropbox credential patterns; they were not copied.
- Several vendor folders include generated `Processed_Output/` workbooks. These were treated as sample-output evidence only and were not modified.
- Internet/fiber vendors are present in `Internet - Telecom`, but EPB Fiber training files are under `Electricity - Power/EPB Fiber Optics`.
- Trash fixtures remain handled through the canonical invoice fixture layer, not this utility processor expansion.
