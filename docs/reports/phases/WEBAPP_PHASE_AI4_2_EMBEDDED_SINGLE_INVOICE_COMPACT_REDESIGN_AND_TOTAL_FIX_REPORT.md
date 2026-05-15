# Phase AI-4.2 — Embedded Single Invoice View Compact Redesign + Correct Invoice Total Display

Date: 2026-05-11

## 1. Scope

This phase focused on the embedded/in-workspace Single Invoice View. The detached popout behavior, Bulk Mode, AI-assisted processing path, deterministic Richmond/HWEA processors, Dropbox, source documents, and `Output/Template.xlsx` were not changed.

## 2. Embedded Layout Redesign

The embedded Single Invoice View was tightened so it fits naturally inside the Template panel instead of behaving like a large form page.

Changes:

- Reduced header height, body gaps, card padding, field heights, and line-item row height.
- Hid the separate AI mapping review panel while Single Invoice Mode is active because the same review work now lives inside the invoice view.
- Changed the embedded invoice/context summary to a single-column stack to avoid horizontal overflow inside the narrower Template panel.
- Kept the detached popout using a roomier two-column layout through `body.popout-mode` CSS overrides.
- Prevented the Single Invoice card from flex-stretching like the Bulk table card.

## 3. Compact Visual System

The visual treatment was made calmer and more accounting-tool-like:

- Neutral gray panels replaced heavy bordered cards.
- Warning states are now subtle chips/rows instead of dominant orange outlined blocks.
- Review issue rows are compact and scan-friendly.
- Buttons and inputs use smaller heights and tighter spacing.
- AI validation flags are neutral soft chips.

## 4. Stretch / Deformation Fixes

Root cause:

- `.template-workspace .card` globally made template cards `flex: 1 1 auto`.
- The Single Invoice card inherited that behavior, so embedded cards stretched vertically.
- The summary grid required more horizontal width than the embedded Template panel had.

Fix:

- Added a specific `.template-workspace .single-invoice-mode.card` override with `flex: 0 1 auto`.
- Embedded summary now stacks instead of forcing two columns.
- Detached mode keeps the wider two-column behavior.
- Verified no horizontal overflow in the embedded Single Invoice card.

## 5. Totals Correction

The Lowe’s AI-assisted invoice now displays the correct financial hierarchy:

- Invoice total: `$6.75`
- Merchandise subtotal: `$6.16`
- Tax: `$0.59`
- ResMan line total: `$6.16`
- Pending tax delta: `$0.59`

Root cause:

- Older AI-assisted result caches stored `total_amount` and `validation_summary.invoice_total` at the invoice level, but not inside each flattened preview row’s `_meta.ai_provenance`.
- The frontend Single Invoice View is row-driven, so it fell back to the row amount `$6.16`.

Fix:

- `GET /api/batches/{batch_id}/preview` now enriches flattened preview row metadata with invoice-level totals when available.
- For older AI caches, tax is inferred as `invoice_total - subtotal` when explicit tax metadata is missing.
- The UI now treats `Invoice total` as the primary total and labels `ResMan line total` separately.

## 6. Tax Section Cleanup

Tax presentation now reads as a compact accounting note:

- `Tax` is shown separately from merchandise.
- `Pending tax` shows the current difference between invoice total and ResMan line total.
- Tax policy labels were simplified:
  - Separate tax line
  - Distribute tax
  - Leave for review

## 7. Property / Context Cleanup

Property/context now appears as a compact summary block:

- Property
- Location
- Service address
- AI confidence
- Source document
- Inline resolve action

The block no longer forces a second column in embedded mode, preventing cramped and overlapping content.

## 8. Review Issue Cleanup

Review tasks remain actionable but lighter:

- Compact task rows
- Short explanations
- Action controls on the right
- No oversized warning boxes
- No separate duplicate AI mapping panel in Single Invoice Mode

## 9. Line Items Compacting

The line item grid now uses:

- Smaller header height
- Smaller row padding
- Compact editable inputs
- Bulk-mode-like header styling
- Subtle row hover / selected state

## 10. Embedded vs Detached Handling

Shared logic remains in `SingleInvoiceMode`.

Mode-specific tuning:

- Embedded: stacked summary, compact spacing, no stretching.
- Detached: roomier popout spacing and two-column summary through `body.popout-mode`.

No business logic was forked.

## 11. Browser Verification

Playwright visual check was used because the browser-use in-app backend was unavailable in this session.

Screenshots:

- `docs/reports/phases/screenshots/phase_ai4_2_embedded_single_invoice/embedded_single_invoice_1600x900.png`
- `docs/reports/phases/screenshots/phase_ai4_2_embedded_single_invoice/detached_single_invoice_1366x768.png`

Verified with the Lowe’s AI-assisted batch:

- Batch: `QA AI 1.3 Lowes Pro Supply`
- Invoice number: `83690`
- Invoice total: `$6.75`
- Merchandise: `$6.16`
- Tax: `$0.59`
- Embedded Single Invoice card had no horizontal overflow.
- Detached Single Invoice mode still rendered.

## 12. Tests Performed

Frontend:

- `cd webapp/frontend`
- `npm.cmd run build` — passed
- `npx.cmd tsc --noEmit` — passed
- `npm.cmd run test:e2e` — passed: 21 passed, 2 skipped

Backend:

- `python -m compileall webapp\backend` — passed
- `python scripts\verify_backend_routes.py` — passed
- `python scripts\smoke_ai_openai_compatible_provider.py` — passed
- `python scripts\smoke_ai_mapping_review.py` — passed

Added E2E coverage:

- Lowe’s AI-assisted Single Invoice View displays invoice total separately from merchandise and tax.
- Single Invoice Mode does not render the duplicate AI mapping review panel.

## 13. Integrity Notes

- No Dropbox calls were triggered.
- No source documents were modified.
- `Output/Template.xlsx` was unchanged.
- The Lowe’s batch cache was read but not mutated by the screenshot verification.
- Deterministic vendor processors were not changed.

## 14. Remaining Limitations

- Tax policy selection still records the chosen policy and resolves review state, but full row-level tax redistribution remains a future enhancement.
- The embedded Template panel can still become visually dense at very narrow widths because it shares space with Batches and Document panels.
- Durable invoice-level `Ready to export` persistence remains a future phase.

## 15. Next Recommended Phase

Phase AI-4.3 should implement persisted invoice review status and full tax treatment application:

1. Persist `Reviewed` / `Ready to export` status in the active revision snapshot.
2. Apply proportional tax distribution to editable preview rows when selected.
3. Add export gating for unresolved blocking review tasks.
4. Add richer property/location learned mapping application during future AI-assisted processing.
