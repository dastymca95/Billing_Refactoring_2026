# Phase 1W - Batch File Manager Redesign + Per-File Actions + Batch Drop Target Upload

Date: 2026-05-03  
Stack used: backend `http://localhost:8001`, frontend `http://localhost:5174`

## Scope

Phase 1W focused on the operator-facing batch/file sidebar. The goal was to verify the current UI in a real browser, fix the visible issues safely, and add regression coverage for the redesigned batch manager.

No vendor extraction logic, Richmond/Hopkinsville processors, Dropbox code, AI provider code, `Output/Template.xlsx`, source PDFs/CSVs, or `.env` were intentionally modified.

## Browser QA Performed

Opened the live app at `http://localhost:5174` with the in-app browser and Playwright. Verified the frontend proxy returned the same batch data as the clean backend at `http://localhost:8001`.

Before screenshot:

- `docs/reports/phases/screenshots/phase_1w_batch_file_manager/before/batch_manager_before_full_workspace.png`

After screenshots:

- `docs/reports/phases/screenshots/phase_1w_batch_file_manager/after/batch_manager_after_full_workspace_fixed.png`
- `docs/reports/phases/screenshots/phase_1w_batch_file_manager/after/file_row_hover_after_fixed.png`
- `docs/reports/phases/screenshots/phase_1w_batch_file_manager/after/delete_file_confirm_modal.png`
- `docs/reports/phases/screenshots/phase_1w_batch_file_manager/after/empty_batch_expanded.png`
- `docs/reports/phases/screenshots/phase_1w_batch_file_manager/after/drag_over_batch_row.png`
- `docs/reports/phases/screenshots/phase_1w_batch_file_manager/after/after_drop_uploaded_file.png`
- responsive/E2E screenshots under `docs/reports/phases/screenshots/phase_1w_batch_file_manager/e2e/`

## Bugs Found

1. File-row delete buttons rendered as blank gray squares.
   - DOM showed the buttons were real delete actions.
   - Browser screenshot showed no trash icon.
   - Headless Playwright inspection showed the inner SVG rendered with width `0`.

2. Process was still a floating sidebar action instead of batch-contextual.
   - The browser showed a generic Process button above the batch list.
   - That made it unclear which batch would be processed.

3. Inactive expanded batch file loading had no explicit error state.
   - Source review showed errors were swallowed into an empty file list.
   - A hanging request could leave skeleton loading visible indefinitely.

4. Drag/drop onto a batch row was not implemented.
   - Upload worked through the active Add files affordance only.
   - Dropping on a batch folder row did not target that batch.

5. Disabled nav items made the app look unfinished.
   - Review, Vendors, Exports, and Settings were visible but inactive.

6. File type badges were too visually loud.
   - PDF badges used a strong red treatment in the batch sidebar.

## Fixes Implemented

### Batch Explorer

Updated `webapp/frontend/src/components/BatchExplorer.tsx`:

- Added direct batch-row drop targets.
- Added drag-over highlight state.
- Added per-batch Process buttons.
- Added file loading timeout guard.
- Added explicit file-load error state: `Could not load files.` with Retry.
- Added empty state: `No files in this batch.`
- Allowed expanded inactive batches to show files without forcing a switch.
- Allowed file selection and file deletion from a specific batch row.
- Kept inline double-click batch rename.
- Kept app-native delete confirmation via App-level handlers.

### App Wiring

Updated `webapp/frontend/src/App.tsx`:

- Added targeted upload flow: dropping files onto a batch uploads into that batch and switches to it after success.
- Added targeted file selection from non-active expanded batches.
- Added targeted file deletion wiring that refreshes the correct batch file list.
- Refactored processing so a Process click on a specific batch processes that batch id, not a stale active id.
- Preserved app-native confirmation for destructive actions.

### Sidebar Action Bar

Updated `webapp/frontend/src/components/BatchActionsBar.tsx`:

- Removed the floating/global Process action.
- Kept Stop only while processing.
- Kept More actions for preview/export/delete active batch utilities.

### Visual Polish

Updated `webapp/frontend/src/styles.css`:

- Reworked batch folder rows for active/open/hover/drag-over states.
- Made New batch more visible and product-grade.
- Made file rows more compact.
- Switched PDF/file type chips to neutral styling.
- Fixed zero-width trash SVG rendering with explicit SVG dimensions.
- Made trash actions visible and recognizable.

### Nav Simplification

Updated `webapp/frontend/src/components/NavRail.tsx`:

- Hid inactive Review/Vendors/Exports/Settings items.
- Kept a minimal active Batches navigation entry.
- Added `data-testid="nav-rail"` for regression tests.

### Route Verification

Updated `scripts/verify_backend_routes.py`:

- Added upload/list-files/delete-file route checks:
  - `POST /api/batches/{batch_id}/upload`
  - `GET /api/batches/{batch_id}/files`
  - `DELETE /api/batches/{batch_id}/files/{filename}`

The backend delete-file endpoint already existed and was not rewritten in this phase.

## E2E Tests Added/Updated

Updated `webapp/frontend/e2e/operator-visual.spec.ts` to target the current BatchExplorer UI instead of retired BatchHeader dropdown selectors.

Coverage now includes:

- Template header responsive screenshots at 1920x1080, 1600x900, and 1366x768.
- Batch explorer renders.
- Disabled nav items are hidden in the nav rail.
- Expanded batch shows files or empty state without endless skeletons.
- Batch row click switches active batch.
- New batch modal opens and validates long names.
- Inline batch rename opens and cancels.
- File delete opens app-native confirmation and can be cancelled.
- Drag/drop onto a batch row uploads a generated QA `.txt` file into that batch.
- Column view controls remain visible and non-duplicated.
- Issues drawer opens/closes.

## Test Results

Passed:

- `cd webapp/frontend && npm.cmd run build`
- `cd webapp/frontend && npm.cmd run test:e2e`
  - 12 passed
- `python -m compileall webapp\backend`
- `python scripts\verify_backend_routes.py`

Route verifier output included:

- `DELETE /api/batches/{batch_id}/files/{filename}`
- `GET /api/batches/{batch_id}/files`
- `POST /api/batches/{batch_id}/upload`

Integrity checks:

- `git status --short -- Output\Template.xlsx "Training Bills_Invoices" .env` returned no modifications.
- No AI calls were made.
- No Dropbox workflow was triggered.
- File delete was visually tested through the confirmation modal and cancelled in-browser.

## QA Data Created

The drag/drop E2E created QA-only batches named `QA Drop Target ...` and uploaded a generated safe text file named `phase1w_drop_test.txt`.

These are webapp QA fixture batches under the webapp data area. No real source bills were deleted or modified.

## Deferred Items

1. File rename remains deferred.
   - The UI now has clear delete/open actions.
   - Rename file is riskier because it can affect preview/document references and should be handled in a dedicated phase.

2. Real OS-level drag from Windows Explorer should still get one human smoke check.
   - Playwright verified the browser drag/drop event path and upload endpoint with a generated `File`.
   - External shell-to-browser drag is not fully reproducible in headless automation.

3. Processing was not clicked in E2E.
   - The row-level Process target is wired and visible.
   - Avoided running vendor processors/Dropbox-adjacent flows during this visual/sidebar phase.

4. File-load failure state was implemented but not forced through a backend fault injection.
   - Empty state and no-skeleton regressions are covered.
   - A future test can mock or intercept `/files` failure if the test harness adds request interception helpers.

## Next Recommended Phase

Run a narrow Phase 1X focused on destructive QA and file lifecycle hardening:

- file delete success on QA-created files only
- file rename design and endpoint, if still desired
- stale preview/document selection after file deletion
- actual Windows Explorer drag/drop manual smoke
- process row action smoke on a QA unsupported-only batch
