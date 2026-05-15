# Phase QA-1 - Full Batch Regression, Canonical Description Enforcement, and Mandatory Field Compliance

Date: 2026-05-14

## 1. Scope

Phase QA-1 audited existing webapp batches, deterministic utility processors, canonical AI fixtures, and browser views for Bulk Mode and Single Invoice Mode. The main regression target was the recurring description bug where utility rows used the property/community name after the service period instead of the real service address from the bill.

The phase also hardened mandatory field validation, Proper Case formatting, stale preview normalization, and screenshot/image behavior for weak OCR cases such as Weakley County Municipal Electric System.

## 2. Batches Inspected

Machine-readable inventory:

`webapp_data/qa/full_batch_regression_20260514_125803.json`

Summary:

- Batches inspected: 60
- Cached previews found: 42
- Cached preview rows inspected: 455
- Cached manual review rows: 95
- Active deterministic utility vendor overlays: 26

Top cached-preview contract findings:

- `line_item_description_not_proper_case`: 451
- `invoice_description_not_proper_case`: 411
- `invoice_description_missing_service_address`: 25
- `property_abbreviation_missing`: 9
- `invoice_description_contains_city_state_zip`: 8
- `gl_account_missing`: 7

Important note: these are cached historical preview rows, many produced before QA-1. The preview and manual review API paths now normalize cached results on read, and fresh deterministic utility reprocessing passed the stricter contract.

## 3. Vendors Inspected

Fresh dry-run and browser QA covered:

- Alabama Power
- Tennessee American Water
- EPB Fiber Optics
- Kentucky Utilities
- Knoxville Utility Board
- Clarksville Gas and Water
- The City of Henderson
- HWEA / Hopkinsville Water Environment Authority
- Richmond Utilities
- Weakley County Municipal Electric System image bill

Canonical fixture coverage also passed:

- Capital Waste
- EPB
- Lowe's Pro Supply
- Spectrum
- TK Elevator
- Servall Pest remains skipped with an explicit missing-source reason.

## 4. Failures Found

Systemic causes:

- The shared utility row builder could fall back from service address to matched property name when building descriptions.
- Legacy HWEA description rendering preserved city/state/ZIP in output descriptions.
- Some cached previews predated Proper Case normalization and still contained historical all-caps/lowercase rows.
- AI/canonical fixture expectations still allowed city/state/ZIP in EPB descriptions and older non-Proper Case descriptions.
- The new service-address description builder was initially too broad and applied service-address-only formatting to non-utility purchases such as Lowe's Pro Supply and TK Elevator.

## 5. Fixes Implemented

New shared modules:

- `utils/text_normalization.py`
- `webapp/backend/services/description_builder.py`
- `webapp/backend/services/output_contract_validator.py`

Updated integration points:

- `utils/text_normalize.py`
- `webapp/backend/services/utility_processor_common.py`
- `webapp/backend/services/utility_wave2_processors.py`
- `webapp/backend/services/row_normalizer.py`
- `webapp/backend/services/canonical_rules.py`
- `webapp/backend/services/ai_invoice_processor.py`
- `webapp/backend/api/processing.py`
- HWEA legacy processor formatting path

New smoke scripts:

- `scripts/smoke_description_contract.py`
- `scripts/smoke_required_fields_contract.py`
- `scripts/smoke_full_batch_regression.py`

Frontend test hardening:

- `webapp/frontend/e2e/ingestion-ai9.spec.ts` now has enough timeout headroom for upload, ingestion preview, UI screenshot, and cleanup.

## 6. Description Contract

For utility/service rows, descriptions are now built from validated components:

- Invoice Description: `{service_period_range} - {unit + service_address OR service_address}`
- Line Item Description: `{service_period_range} - {unit + service_address OR service_address} - {source_service_line_description}`

Rules now enforced:

- Service address wins over property/community name when present.
- City/state/ZIP is stripped from ResMan-facing descriptions.
- Unit/location is included only when valid and available.
- Property-name fallback is reserved for true property-level categories or explicit property-level services.
- Non-utility purchase categories keep their category-specific summary style.

Critical regressions checked:

- Alabama Power: description uses service address.
- Tennessee American Water: description uses service address.

## 7. Proper Case

The new formatter applies Excel-PROPER-style output while preserving acronyms and operational terms:

- Preserved: EPB, HWEA, CPWS, KUB, TVA, GL, ID, LLC, TK
- Normalized examples: `1050 DENZIL DR` -> `1050 Denzil Dr`
- Normalized examples: `FUEL RECOVERY ADJUSTMENT` -> `Fuel Recovery Adjustment`

The formatter avoids changing invoice numbers, GL codes, URLs, and account-like tokens.

## 8. Mandatory Field Validation

The row contract validator checks:

- Invoice Number
- Bill or Credit
- Invoice Date
- Accounting Date
- Vendor
- Invoice Description
- Line Item Number
- Property Abbreviation
- GL Account
- Line Item Description
- Amount
- Expense Type
- Is Replacement Reserve
- Due Date

It also flags:

- invalid GL
- raw address in Location
- property name used where service address should be used
- city/state/ZIP in descriptions
- standalone tax lines
- payment/previous balance lines as expenses
- missing Document Url when explicitly required outside dry-run contexts

## 9. Weakley / Screenshot Behavior

Weakley image routing remains deterministic. In fresh QA:

- Invoices: 0
- Rows: 0
- Manual review items: 1

This is the intended safe behavior for weak OCR: the app does not fabricate ready/exportable rows. Missing fields remain review-blocking instead of silently passing through AI or fake rows.

## 10. Bulk / Single Consistency

Fresh U4 utility e2e output smoke passed for representative utility vendors. Browser e2e then verified Bulk Mode and Single Invoice Mode render from the same preview data for all U4 cases.

Representative screenshots copied to:

`docs/reports/phases/screenshots/phase_qa1_full_batch_regression/`

Included examples:

- `alabama_power_bulk.png`
- `alabama_power_single.png`
- `tennessee_american_water_bulk.png`
- `tennessee_american_water_single.png`
- `weakley_image_bulk.png`
- `weakley_image_manual_review.png`
- `kentucky_utilities_bulk.png`
- `knoxville_single.png`
- `hwea_single.png`
- `richmond_single.png`
- `ai9_file_type_badges.png`

## 11. Tests Performed

Backend:

- `python -m compileall webapp\backend`
- `python scripts\verify_backend_routes.py`
- `python scripts\smoke_document_ingestion.py`
- `python scripts\smoke_canonical_rules_engine.py`
- `python scripts\smoke_canonical_invoice_fixtures.py`
- `python scripts\smoke_utility_processors.py`
- `python scripts\smoke_utility_e2e_outputs.py`
- `python scripts\smoke_utility_e2e_outputs.py --prepare-browser-fixtures`
- `python scripts\smoke_description_contract.py`
- `python scripts\smoke_required_fields_contract.py`
- `python scripts\smoke_full_batch_regression.py`
- `python scripts\smoke_ai_openai_compatible_provider.py`
- `python scripts\smoke_ai_mapping_review.py`

Frontend:

- `npm.cmd run build`
- `npx.cmd tsc --noEmit`
- `npm.cmd run test:e2e`

Results:

- Backend route contract: PASS
- Document ingestion smoke: PASS
- Canonical rules smoke: PASS
- Canonical invoice fixtures: PASS
- Utility processors: PASS
- Utility e2e golden outputs: PASS
- Description contract smoke: PASS
- Required fields contract smoke: PASS
- Full batch inventory/cache audit: PASS
- AI OpenAI-compatible provider smoke: PASS
- AI mapping review smoke: PASS
- Frontend build/typecheck/e2e: PASS, 35/35 Playwright tests

## 12. Integrity

- `Output/Template.xlsx` was not modified.
- `.env` was not modified.
- Source bill files were not modified.
- Old Scripts were not modified.
- Dropbox was skipped in automated dry-run tests.
- No API keys were exposed.

Note: a legacy HWEA processor code file under `Training Bills_Invoices/.../process_hopkinsville_water_environment_authority.py` was updated as processor code, not as a source bill/training document.

## 13. Remaining Limitations

- Historical cached `_webapp_result.json` files may still contain old raw rows. The API now normalizes preview/manual-review reads, but regenerating every historical cache would mutate user batch artifacts, so QA-1 records those as inventory rather than rewriting them.
- Weakley image bills remain dependent on OCR/vision quality. They now fail safely with review blockers instead of producing fake ready rows.
- HWEA and Richmond still have expected manual-review items for scanned/multi-invoice ambiguities, mainly unit mapping and dry-run support-link status.

## 14. Next Recommended Phase

Phase QA-2 should add an operator-facing "Regenerate Preview with Current Rules" action per batch. That would let the user update old cached previews intentionally, using the QA-1 contract, without silently mutating historical batch artifacts.
