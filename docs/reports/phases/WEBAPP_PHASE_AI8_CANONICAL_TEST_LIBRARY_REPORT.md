# Phase AI-8 - Canonical Test Library + Difficult Vendor Fixtures

Date: 2026-05-13

## 1. Fixture Architecture

Phase AI-8 adds a deterministic canonical invoice fixture library under:

`webapp/backend/tests/fixtures/canonical_invoices/`

Each fixture has:

- `source_reference.json` - cached extraction candidates and source metadata.
- `expected.yaml` - golden expected ResMan/canonical output.

The backend service `webapp/backend/services/canonical_invoice_fixtures.py` loads fixtures, runs the same AI validation/canonicalization/template-row builder used by real AI-assisted invoices, and compares expected vs actual without calling external AI, Dropbox, or export.

Fixture test output includes:

- expected values
- actual normalized/canonical values
- grouped pass/fail checks
- rows generated for Bulk Mode
- review flags
- total reconciliation state
- reasoning timeline

## 2. Fixtures Added

Complete fixtures:

- `capital_waste`
- `spectrum`
- `lowes_pro_supply`

Registered incomplete placeholders:

- `servall_pest`
- `tk_elevator`
- `epb`

Incomplete fixtures are listed in the UI and smoke output, but skipped by default until stable cached extraction payloads and expected values are added.

## 3. Spectrum Expected Behavior

The Spectrum fixture validates:

- vendor: `Spectrum Business & Community Services`
- category: `subscriptions`
- invoice number: `0310675042426`
- account: `8363 29 023 0310675`
- invoice date: `04/24/2026`
- due/autopay date: `05/11/2026`
- service period: `04/24/26-05/23/26`
- property: `AMA`
- location: blank
- GL: `6905`
- lines:
  - `Community Solutions Services` = `10836.00`
  - `Taxes, Fees and Charges` = `235.51`
- zero-dollar Xumo/Spectrum promotional line excluded
- total: `11071.51`

## 4. Lowe's Expected Behavior

The Lowe's Pro Supply fixture validates:

- vendor: `Lowes Pro Supply`
- category: `other_infrequent`
- invoice number: `83690`
- invoice total: `6.75`
- merchandise subtotal: `6.16`
- tax: `0.59`
- GL is numeric only: `6651`
- Location remains blank, never a raw address
- zero-dollar promotional line excluded

The current default tax policy distributes the tax/difference to the ResMan payable line, so the exported row amount is expected as `6.75`.

## 5. Test Bench UI Updates

Canonical Rules Studio now includes:

- fixture selector
- `Run selected`
- `Run all fixtures`
- grouped result table:
  - Identity
  - Dates
  - Vendor / Property
  - GL
  - Descriptions
  - Amounts
  - Review flags
- suite summary for all fixtures
- JSON export for selected or suite results

The UI calls:

- `GET /api/canonical-rules/test-fixtures`
- `POST /api/canonical-rules/test-bench`

## 6. Smoke Script Results

New script:

`scripts/smoke_canonical_invoice_fixtures.py`

Observed output:

```text
capital_waste: PASS
epb: SKIPPED
lowes_pro_supply: PASS
servall_pest: SKIPPED
spectrum: PASS
tk_elevator: SKIPPED
Canonical invoice fixture smoke passed.
```

## 7. Incomplete Fixtures

The following are intentionally incomplete:

- `servall_pest`
- `tk_elevator`
- `epb`

They exist so the UI/test bench already has slots for the difficult vendors, but they will not fail CI/smoke until their expected output is made complete.

## 8. Backend/API Changes

Added:

- `webapp/backend/services/canonical_invoice_fixtures.py`
- `GET /api/canonical-rules/test-fixtures`
- `POST /api/canonical-rules/test-bench` now supports:
  - `fixture_key`
  - `run_all`
  - existing dry-run draft patches

Updated:

- `scripts/verify_backend_routes.py`
- `scripts/smoke_canonical_rules_studio.py`
- `webapp/backend/services/ai_invoice_processor.py` now preserves an explicit `category` candidate into canonicalization.

## 9. Tests Performed

Frontend:

- `npm.cmd run build` - passed
- `npx.cmd tsc --noEmit` - passed
- `npm.cmd run test:e2e` - passed: 23 passed, 1 skipped

Backend:

- `python -m compileall webapp\backend` - passed
- `python scripts\verify_backend_routes.py` - passed
- `python scripts\smoke_canonical_rules_engine.py` - passed
- `python scripts\smoke_capital_waste_invoice.py` - passed
- `python scripts\smoke_canonical_rules_studio.py` - passed
- `python scripts\smoke_canonical_invoice_fixtures.py` - passed
- `python scripts\smoke_ai_openai_compatible_provider.py` - passed
- `python scripts\smoke_ai_mapping_review.py` - passed

Integrity:

- No Dropbox calls were made.
- No real external AI calls were required by fixture tests.
- `Output/Template.xlsx` was not modified by this phase.
- `.env` was not modified.
- Source bills/PDFs/CSVs were not modified.

## 10. Limitations

- Spectrum is modeled as `subscriptions` because the current canonical category set does not yet include a dedicated cable/internet category.
- Servall, TK Elevator, and EPB remain placeholder fixtures until stable cached extraction payloads are captured.
- Fixture execution is reference-heavy and can take tens of seconds on a cold backend process.

## 11. Next Recommended Phase

Phase AI-9 should add complete fixtures for:

- Servall Pest
- TK Elevator
- EPB scanned PDF
- A-1 Heating and Air
- Rasa Floors
- HD Supply

It should also add a small in-app action to capture the current processed invoice as a draft fixture, so future difficult vendor examples can become regression tests directly from the operator workflow.
