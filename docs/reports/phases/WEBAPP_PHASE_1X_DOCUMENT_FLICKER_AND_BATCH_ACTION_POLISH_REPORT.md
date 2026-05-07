# Phase 1X - Document Flicker and Batch Action Polish

Date: 2026-05-03  
Stack used: backend `http://localhost:8001`, frontend `http://localhost:5174`

## Scope

This was a focused patch for three UX issues in the Batch/File Manager and document preview:

1. PDF/document preview flicker when switching batches/files.
2. Sticky blue drag/drop state after dropping files onto a batch row.
3. Batch/file action polish for Process, Delete file, and New batch.

No vendor extraction logic, Dropbox workflow, AI provider logic, backend business rules, `Output/Template.xlsx`, source PDFs/CSVs, or `.env` were changed.

## Root Cause - Document Flicker

`DocumentPreviewPanel` cleared `preview` immediately whenever `batchId` or `filename` changed. That unmounted the current `PdfWorkspace`, so the next PDF canvas started with no measured page dimensions. The visible result was a brief zero/small canvas or blank page before PDF.js measured the new page and resized the canvas to the correct full dimensions.

The lower-level `PdfPageCanvas` already rendered new pages offscreen and cancelled stale render tasks, but that protection was defeated during file switches because the parent panel unmounted the workspace before the new preview metadata was available.

## Document Flicker Fix

Updated `webapp/frontend/src/components/DocumentPreviewPanel.tsx`:

- Keeps the previous rendered document mounted while the next file preview metadata loads.
- Shows a stable local loading overlay over the previous document instead of clearing the body.
- Only swaps `PdfWorkspace` to the new file after `api.filePreview(...)` returns for the current request.
- Ignores stale preview fetches with the existing cancellation flag.
- Adds a reserved skeleton page for the no-previous-document case.

Updated `webapp/frontend/src/components/pdf_workspace/PdfPageCanvas.tsx`:

- Tracks whether a first rendered frame exists.
- Uses a full-page first-frame placeholder instead of a zero-size canvas.
- Keeps the existing rendered canvas visible while a new page/file render completes.
- Maintains render cancellation and stale-render protection.

Browser measurement at 1366x768:

- During file switch canvas wrapper: `612 x 792`
- After file switch canvas wrapper: `612 x 792`

This confirms the page no longer jumps from tiny to full-size in the tested PDF switch path.

## Root Cause - Sticky Drag/Drop State

The batch explorer kept `dragOverBatchId` as local visual state and cleared it on normal drag leave/drop. In real drops onto child elements or while an async upload/switch was running, drag leave was not guaranteed to fire, and the row could retain the `.drag-over` class longer than intended.

## Drag/Drop Fix

Updated `webapp/frontend/src/components/BatchExplorer.tsx`:

- Clears drag state whenever the active batch changes.
- Clears drag state on global `dragend` and `drop`.
- Wraps targeted upload/drop handling in `try/finally` so `dragOverBatchId` is cleared even if upload or batch switch fails.
- Keeps the normal active/open styling after a successful drop, without retaining `.drag-over`.

Browser verification:

- Before drop class: `batch-row   drag-over`
- After drop class: `batch-row active open`

## Action Polish Changes

Updated `webapp/frontend/src/components/BatchExplorer.tsx` and `webapp/frontend/src/styles.css`:

- Process button is now compact icon-style with tooltip `Process batch`.
- Process text remains in DOM only as hidden label; the visible button is a minimal play icon.
- Delete file hover no longer uses a red filled box.
- Delete hover is now a subtle neutral gray background with darker/redder icon color only.
- `+ New` was changed to `+ New batch` and kept at the Batches section header level.
- Active batch styling is slightly less saturated so it does not resemble a stuck drop target.

## E2E Updates

Updated `webapp/frontend/e2e/operator-visual.spec.ts`:

- Added assertion that the Process button is compact and titled `Process batch`.
- Added neutral delete-hover style test.
- Added assertion that `.drag-over` clears after batch-row drop.
- Existing browser coverage still verifies batch explorer rendering, row switching, app-native file delete confirmation, drag/drop upload, template controls, and issues drawer.

## Screenshots

Saved under:

`docs/reports/phases/screenshots/phase_1x_document_flicker_batch_actions/`

Files:

- `after_action_polish_loaded_document.png`
- `after_document_stable_loaded.png`
- `during_document_switch_stable_overlay.png`
- `after_document_switch_rendered.png`
- `drag_over_before_drop.png`
- `drag_highlight_cleared_after_drop.png`

## Tests Performed

Passed:

- `cd webapp/frontend && npm.cmd run build`
- `cd webapp/frontend && npm.cmd run test:e2e`
  - 13 passed
- `python -m compileall webapp\backend`
- `python scripts\verify_backend_routes.py`

Manual/browser QA:

- Opened `http://localhost:5174/`.
- Verified the batch sidebar shows `+ New batch`.
- Verified Process is a compact play-style button with `Process batch` tooltip/accessible label.
- Verified file delete icons remain visible and use a neutral hover treatment.
- Switched PDF files in a multi-PDF HWEA batch and measured stable document dimensions.
- Simulated drag/drop onto a QA-created batch row and verified `.drag-over` clears after drop.

Integrity:

- `git status --short -- Output\Template.xlsx "Training Bills_Invoices" .env` returned no modifications.
- No AI calls were made.
- No Dropbox workflow was triggered.
- E2E drag/drop used generated QA text files in QA-created batches only.

## Limitations

- The document flicker was verified with browser automation and screenshots at 1366x768. A human smoke check in the user’s exact window size is still useful because responsive pane visibility depends on viewport width.
- Real Windows Explorer drag/drop was not physically performed; Playwright verified the browser drag/drop event path with generated files.
