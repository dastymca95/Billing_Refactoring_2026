# Phase AI-7 - Canonical Rules Studio + Rule Test Bench

Date: 2026-05-13

## 1. UI Overview

Implemented a new **Canonical Rules** section inside the existing Settings window.

The Studio shows invoice categories as first-class business objects:

- utilities
- pest_control
- landscaping
- marketing
- subscriptions
- trash_collection_services
- other_infrequent
- unknown

For each category, the UI presents human-readable rule groups instead of raw YAML:

- Identity / category detection
- Required fields
- Date rules
- Description rules
- Property / location rules
- GL mapping rules
- Tax / fee handling
- Previous balance / payment exclusion
- Document URL rules
- Manual review triggers

The right-side editor exposes only compact, whitelisted controls:

- vendor keywords
- service keywords
- location policy
- default GL candidates
- ignored line keywords
- invoice description format
- line item description format

## 2. Backend Routes

Added `webapp/backend/api/canonical_rules.py` and registered it in `webapp/backend/main.py`.

Routes added:

- `GET /api/canonical-rules`
- `GET /api/canonical-rules/{category}`
- `POST /api/canonical-rules/validate`
- `PATCH /api/canonical-rules/{category}`
- `POST /api/canonical-rules/restore`
- `POST /api/canonical-rules/import-preview`
- `POST /api/canonical-rules/import-apply`
- `POST /api/canonical-rules/test-bench`

The route verifier now includes these routes.

## 3. Editable Vs Read-Only Sections

Editable from the app:

- category labels
- vendor keywords
- service keywords
- default GL candidate mappings
- fee handling mappings
- ignored line keywords
- location policy
- AI/validation flags
- category invoice description format
- category line item description format

Read-only in this phase:

- raw YAML structure
- required base categories
- mandatory project-level required fields
- low-level canonical engine internals
- source `Canonica rules.xlsx` unless explicitly imported

Unknown YAML fields are preserved by loading the full runtime rule document, applying only whitelisted mutations, backing up first, and atomically replacing the file.

## 4. Test Bench Architecture

Added `webapp/backend/services/canonical_rules_studio.py`.

The Test Bench runs a deterministic dry-run path:

1. Load built-in Capital Waste extraction candidates.
2. Optionally apply unsaved UI edits in memory only.
3. Validate through the AI invoice normalization layer.
4. Apply canonical rules.
5. Build ResMan rows.
6. Compare expected vs actual.
7. Return reasoning timeline, rows, review flags, checks, and pass/fail.

No batches, Dropbox uploads, exports, or source files are modified.

## 5. Capital Waste Test

Built-in expected result:

- category: `trash_collection_services`
- vendor: `Capital Waste Services`
- invoice number: `3150854`
- invoice date: `04/30/2026`
- due date: `05/30/2026`
- property: `RCC`
- location: blank
- GL: `6940`
- line amounts: `365.40`, `34.93`
- total: `400.33`
- previous balance/payment ignored
- invoice description: `05/01/26-05/31/26 - River Canyon Apartments`

Result: pass.

## 6. Excel Import Behavior

Runtime source of truth remains:

`config/canonical_rules.yaml`

`Canonica rules.xlsx` is now an optional authoring source:

- `import-preview` reads the workbook without writing YAML.
- UI shows changed categories, imported row count, and validation result.
- `import-apply` creates a backup first, validates, then updates runtime YAML.
- No silent overwrite occurs.

## 7. Reasoning Timeline

The Test Bench returns a timeline intended to mirror human review:

1. Document read
2. Vendor detected
3. Category classified
4. Canonical rules loaded
5. Property matched
6. Location policy applied
7. GL selected
8. Descriptions composed
9. Totals reconciled
10. Review tasks generated

Example explanation included:

`Location left blank because trash_collection_services allows property-level service when no unit is provided.`

## 8. Tests Performed

Frontend:

- `npm.cmd run build` - passed
- `npx.cmd tsc --noEmit` - passed
- `npm.cmd run test:e2e` - passed, 23 passed / 1 skipped

Backend:

- `python -m compileall webapp\backend` - passed
- `python scripts\verify_backend_routes.py` - passed
- `python scripts\smoke_canonical_rules_engine.py` - passed
- `python scripts\smoke_capital_waste_invoice.py` - passed
- `python scripts\smoke_canonical_rules_studio.py` - passed
- `python scripts\smoke_ai_openai_compatible_provider.py` - passed
- `python scripts\smoke_ai_mapping_review.py` - passed

New test coverage:

- canonical rules route contract
- invalid category rejection
- validation catches missing required fields
- patch creates backup
- restore works
- Capital Waste Test Bench passes
- dry-run rule edit changes result without writing YAML
- `Output/Template.xlsx` unchanged
- Playwright E2E verifies the canonical rules test bench endpoint

## 9. Limitations

- The UI edits category-level canonical controls, not every possible low-level YAML knob.
- The first built-in Test Bench fixture is Capital Waste only.
- Excel import preview reports changed categories, not a full line-by-line YAML diff.
- The Test Bench is deterministic and does not call real AI.
- Word/Excel document ingestion remains outside this phase.

## 10. Next Recommended Phase

Phase AI-8 should add:

- more built-in category fixtures, especially utilities, pest control, landscaping, subscriptions, and maintenance suppliers
- per-vendor and per-property rule overrides inside the same Studio
- a richer import diff viewer for `Canonica rules.xlsx`
- browser UI tests that open the Settings window and interact with Canonical Rules Studio directly
- operator-facing “why this row was generated” explanations linked from Bulk and Single Invoice modes
