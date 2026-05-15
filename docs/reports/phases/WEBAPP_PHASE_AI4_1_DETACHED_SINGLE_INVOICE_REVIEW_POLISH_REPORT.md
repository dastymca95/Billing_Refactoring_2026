# Phase AI-4.1 — Detached Single Invoice View Polish + Required Field Resolution

Date: 2026-05-10

## 1. Scope

This phase polished the AI-assisted Single Invoice Mode, especially when opened in the detached/popout template window, and added required-field resolution workflows for variable vendor invoices. The work stayed inside the webapp AI review path and did not change Richmond/HWEA deterministic processors, Dropbox, export business logic, `Output/Template.xlsx`, or source bill files.

## 2. Detached View Layout Changes

- Reworked Single Invoice Mode into a compact review workspace with:
  - vendor / invoice title
  - invoice status pill (`Needs review` / `Ready`)
  - source-document and trace actions
  - editable invoice header fields
  - property/context card
  - invoice totals card
  - actionable review tasks
  - editable line-item table
  - `Mark reviewed` and `Ready to export` controls
- Detached template popout is no longer read-only for UI edits. It now uses local edit state and synchronizes cell edits back to the main window through `BroadcastChannel`.
- Mapping actions in the detached view can refresh the detached preview after backend updates.

## 3. Actionable Review Tasks

Passive manual-review text was converted into task cards. Tasks now provide specific actions for:

- Vendor mapping required
- Property mapping required
- Location unresolved
- GL mapping required
- Tax handling requires review
- Zero-dollar line excluded
- Inferred invoice date

Resolved tasks update the invoice status locally, and backend-backed resolutions refresh the preview where applicable.

## 4. Property / Location Resolution

Backend candidate endpoints were added:

- `GET /api/ai-review/property-candidates`
- `GET /api/ai-review/location-candidates`
- `POST /api/batches/{batch_id}/ai-review/property-location`

Behavior:

- Property candidates use known property/unit references and the detected service address.
- Location must be a known unit/location for the selected property.
- Raw full addresses are rejected as `Location`.
- The operator can explicitly leave Location blank after choosing a valid property.
- Applying a property/location updates related invoice rows and removes property/location review flags.

## 5. GL Resolution

Single Invoice Mode now loads GL candidates for rows with missing or invalid GL mappings. Candidate buttons update the line item and call the existing GL review endpoint:

- `POST /api/batches/{batch_id}/ai-review/gl-mapping`

GL values remain validated server-side. Text like `MISCELLANEOUS` or `HARDWARE` is not accepted as a ResMan GL Account.

## 6. Tax Handling UI

The invoice view now separates:

- Merchandise subtotal
- Tax
- Invoice total
- ResMan line total
- Difference

Added backend endpoint:

- `POST /api/batches/{batch_id}/ai-review/tax-policy`

Supported policy values:

- `manual_review`
- `distribute_proportionally`
- `separate_tax_line`
- `exclude_tax`

Choosing a non-manual policy removes tax review flags from the current invoice rows.

## 7. Date Inference and Description

- Existing AI validation metadata for inferred invoice dates is surfaced as a review task.
- Invoice description remains editable in Single Invoice Mode and syncs back to Bulk Mode.
- Totals now prefer backend AI provenance fields such as `invoice_total` and `tax_amount` when available, avoiding confusing header totals.

## 8. Review Status Model

The UI derives invoice status from unresolved blocking tasks:

- `Needs review`: blocking review tasks remain.
- `Ready`: no blocking tasks remain.

`Mark reviewed` clears remaining local tasks for the current review session. `Ready to export` is enabled only after blocking tasks are resolved.

## 9. Sync With Bulk Mode

- Single Invoice edits still update the shared preview edit state used by Bulk Mode.
- Detached popout edits are broadcast back to the main window.
- Main-window edits broadcast to the detached popout when it is open.
- Returning to Bulk Mode shows edited cells in the grid.

## 10. Tests Performed

Frontend:

- `cd webapp/frontend`
- `npm.cmd run build` — passed
- `npx.cmd tsc --noEmit` — passed
- `npm.cmd run test:e2e` — passed: 20 passed, 2 skipped

Backend:

- `python -m compileall webapp\backend` — passed
- `python scripts\verify_backend_routes.py` — passed
- `python scripts\smoke_ai_openai_compatible_provider.py` — passed without real provider calls
- `python scripts\smoke_ai_mapping_review.py` — passed

Additional smoke coverage added:

- property candidate generation from service address
- location candidate generation by property
- raw address rejected as Location
- property/location apply endpoint
- tax-policy apply endpoint
- QA mapping smoke batch cleanup

## 11. Integrity Notes

- No Dropbox calls were triggered.
- No AI provider calls were made by automated tests.
- No API keys were exposed.
- `Output/Template.xlsx` was not modified by this phase.
- Source PDFs/CSVs were not modified.
- QA-only `QA AI mapping smoke` batches left by smoke runs were cleaned up.

## 12. Remaining Limitations

- `Mark reviewed` is currently a UI-state action; a durable reviewed/ready workflow can be persisted in a later phase.
- Property mapping persistence is available, but future auto-application of learned property/location mappings can be made richer.
- Tax policy currently resolves review state but does not yet perform a full proportional row rewrite in the preview grid.
- Vendor resolution in Single Invoice Mode is compact; a richer vendor search drawer could be added if operators need deeper review tools.

## 13. Next Recommended Phase

Phase AI-4.2 should persist invoice review state and implement full tax policy transformations:

1. Save invoice-level reviewed/ready state in batch metadata or revision snapshot.
2. Apply proportional tax distribution into editable preview rows.
3. Add a richer location selector tied to selected property.
4. Add learned property/location mappings into future AI-assisted processing.
5. Add export gating based on unresolved blocking review tasks.
