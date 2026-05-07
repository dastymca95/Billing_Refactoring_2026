# Webapp Phase 1S Browser Visual QA + Template Header/Layout Fix Report

Date: 2026-05-02

## 1. Browser Automation Setup

Phase 1S used the in-app browser automation bridge against the real running app at:

- Backend: `http://localhost:8001`
- Frontend: `http://localhost:5174`

The app was opened in the browser, switched to the existing processed `HWEA` batch (`batch_20260502_170939_992`), and inspected through DOM snapshots plus saved screenshots.

Playwright project setup was added:

- `webapp/frontend/playwright.config.ts`
- `webapp/frontend/e2e/operator-visual.spec.ts`
- `webapp/frontend/package.json` scripts:
  - `npm run test:e2e`
  - `npm run test:e2e:headed`

`@playwright/test` was added to `package.json`, but the package and Chromium browser binary were not installed because installing newly acquired software requires action-time confirmation. The install command remains:

```powershell
cd webapp/frontend
npm.cmd install
npx playwright install chromium
```

Until that install is confirmed and completed, `npm.cmd run test:e2e` fails with:

```text
'playwright' is not recognized as an internal or external command
```

## 2. Before Screenshots

Saved under:

`docs/reports/phases/screenshots/phase_1s_before/`

Files:

- `01_full_workspace.png`
- `02_template_header_summary.png`
- `03_template_grid_top_left.png`
- `04_export_button_area.png`
- `05_sidebar_batch_file_area.png`

## 3. Visual Bugs Reproduced

Using the real browser on the HWEA batch, the template header area showed the reported problem:

- The KPI summary row (`Files / Invoices / Rows / Flagged / Edited / Missing Link / Total`) was compressed into a horizontally scrolling strip.
- A horizontal scrollbar appeared visually in the header/summary area instead of belonging only to the grid.
- The Export button lived inside the KPI strip and could be squeezed by the summary stats.
- The template area had two header concepts at once:
  - outer KPI/control header
  - inner `ResMan template preview` card header
- The inner header also exposed `Show optional cols`, duplicating the outer `Columns: Required / Issues / All` control.
- The result looked mechanical and crowded rather than like a polished internal SaaS workspace.

The before full-workspace screenshot clearly showed the top scrollbar crossing the summary area and the Export action competing with the stats row.

## 4. Template Header / Export Layout Fix

Files changed:

- `webapp/frontend/src/components/TemplateWorkspace.tsx`
- `webapp/frontend/src/components/ResManTemplatePreview.tsx`
- `webapp/frontend/src/styles.css`

Implemented layout:

- New top command bar:
  - Left: `ResMan Template`
  - Small metadata: `9 invoices · 56 rows · $889.43 · 21 issues`
  - Right: issue badge + Export button
- KPI chips are now compact chips that wrap cleanly and do not scroll horizontally.
- Export was removed from the KPI strip and is now a stable top-right command.
- Search/filter controls were separated from the KPI row.
- The duplicate inner `ResMan template preview` header was removed.
- `Show optional cols` was removed from the main operator UI because `Columns: All` now owns the all-columns behavior.
- The table horizontal scrollbar now belongs to the table/grid scroll container only.
- The support document cell label was simplified from the symbol-style link to `Open`.

## 5. Responsive Viewport Results

Browser-verified current viewport:

- The header no longer clips.
- Export is visible and aligned to the right.
- KPI chips wrap into multiple rows when needed.
- The table remains horizontally scrollable in the grid only.
- The sidebar does not overlap the template area.

The requested Playwright viewport tests were added for:

- `1920x1080`
- `1600x900`
- `1366x768`

They are in `webapp/frontend/e2e/operator-visual.spec.ts` and will also save viewport screenshots into the after screenshot directory. They were not executed because Playwright installation is pending confirmation.

## 6. Other Visual Bugs Found / Fixed

Fixed:

- Duplicate optional-column control removed.
- Batch picker rows were confirmed readable in the real browser.
- Batch picker row click switched from the empty batch to HWEA successfully.
- New batch modal was app-native, opened cleanly, and showed long-name validation.
- Rename modal was app-native, opened cleanly, and showed empty-name validation.
- Issues drawer opened and closed cleanly.
- No raw FastAPI JSON was seen during these UI checks.

Observed but not changed:

- At this narrow browser width, the document preview column is hidden by responsive layout, so PDF canvas/toolbar visual QA still needs a wider viewport run through Playwright.
- Remaining native confirmations for delete/stop/discard edits were not replaced in this phase.

## 7. E2E Tests Added

Added Playwright tests:

1. App loads and template header is not clipped at `1920x1080`.
2. App loads and template header is not clipped at `1600x900`.
3. App loads and template header is not clipped at `1366x768`.
4. Export button is visible and enabled when preview rows exist.
5. Batch dropdown opens and row click switches batches.
6. New batch modal opens and validates long names.
7. Rename modal opens and validates empty names.
8. Column view buttons are visible and duplicate optional controls are absent.
9. Issues drawer opens and closes when issues exist.

Stable `data-testid` hooks were added for the tested surfaces.

## 8. Test Results

Passed:

```powershell
cd webapp/frontend
npm.cmd run build
```

Passed:

```powershell
python -m compileall webapp\backend
python scripts\verify_backend_routes.py
```

Failed as expected until installation is confirmed:

```powershell
cd webapp/frontend
npm.cmd run test:e2e
```

Failure:

```text
'playwright' is not recognized as an internal or external command
```

## 9. After Screenshots

Saved under:

`docs/reports/phases/screenshots/phase_1s_after/`

Files:

- `01_full_workspace.png`
- `02_template_header_summary.png`
- `03_template_grid_top_left.png`
- `04_export_button_area.png`
- `05_sidebar_batch_file_area.png`
- `06_new_batch_modal.png`
- `07_rename_batch_modal.png`
- `08_issues_drawer.png`
- `09_document_preview.png`

The after full-workspace screenshot shows the template header bug resolved: title/metadata, issue badge, Export button, KPI chips, controls, and grid are visually separated and no header scrollbar overlaps the summary area.

## 10. Remaining Visual Issues

- Install Playwright and Chromium, then run `npm.cmd run test:e2e` to generate the requested 1920/1600/1366 viewport screenshots.
- Verify PDF toolbar and mark drawing at wider widths where the document pane is visible.
- Replace remaining `window.confirm(...)` flows with app-native confirmation modals:
  - stop processing
  - delete batch
  - discard edits on reprocess/refresh
- Consider a true dev fixture command for visual QA so screenshots can use a synthetic, non-Dropbox preview batch.

## 11. Next Recommended Phase

Phase 1T should complete the browser automation hardening:

1. Confirm and run the Playwright install.
2. Execute the new E2E suite.
3. Capture responsive screenshots from Playwright at the three requested widths.
4. Add app-native confirm dialogs for delete/stop/discard edits.
5. Add a safe preview fixture seed command that avoids vendor processors and Dropbox.
