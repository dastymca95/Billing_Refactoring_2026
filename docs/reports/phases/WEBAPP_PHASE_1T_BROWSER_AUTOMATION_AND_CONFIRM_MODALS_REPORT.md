# Webapp Phase 1T Browser Automation + Confirm Modals Report

Date: 2026-05-02

## 1. Scope

Phase 1T continued from:

`docs/reports/phases/WEBAPP_PHASE_1S_BROWSER_VISUAL_QA_TEMPLATE_LAYOUT_REPORT.md`

Goals completed:

- Installed Playwright tooling and Chromium.
- Ran the browser E2E suite.
- Captured responsive screenshots at `1920x1080`, `1600x900`, and `1366x768`.
- Fixed the E2E setup issue found by the real Chromium run.
- Replaced remaining native `window.confirm(...)` flows with an app-native confirm dialog.
- Added a safe QA fixture seed command that does not invoke vendor processors, Dropbox, AI, OCR, or real bill files.

## 2. Browser Automation Install

Commands run:

```powershell
cd webapp/frontend
npm.cmd install
npm.cmd exec -- playwright install chromium
```

`npx playwright install chromium` could not run because PowerShell blocks `npx.ps1` under the current execution policy. The equivalent `npm.cmd exec -- playwright install chromium` succeeded.

Installed:

- `@playwright/test`
- Playwright Chromium browser
- Playwright headless shell support files
- Playwright FFmpeg/winldd support files

Note: `npm install` reported `5 vulnerabilities (2 moderate, 3 high)`. This phase did not run `npm audit fix` because that can make dependency changes outside the visual QA scope.

## 3. E2E Suite Results

Initial E2E run failed because the test helper preloaded `localStorage` before navigation, but Chromium still loaded the app as `No batch yet`.

Fix:

- The helper now navigates first, writes `localStorage`, reloads, and falls back to selecting the batch via the real batch picker UI.
- The suite now prefers the safe `QA Visual Fixture` batch and falls back to existing processed batches only if needed.

Final result:

```powershell
npm.cmd run test:e2e
```

Result:

```text
10 passed
```

Covered:

1. Template header not clipped at `1920x1080`.
2. Template header not clipped at `1600x900`.
3. Template header not clipped at `1366x768`.
4. Export visible and enabled when preview rows exist.
5. Batch dropdown opens and row click switches batches.
6. New batch modal opens and validates long names.
7. Rename modal opens and validates empty names.
8. Column view buttons are visible and duplicate optional controls are absent.
9. Issues drawer opens/closes.
10. Delete batch uses app-native confirm and can be cancelled.

## 4. Responsive Screenshots

Saved under:

`docs/reports/phases/screenshots/phase_1t_responsive/`

Files:

- `viewport_1920x1080.png`
- `viewport_1600x900.png`
- `viewport_1366x768.png`

Observed:

- Template header remains separated from the grid at all tested widths.
- Export remains visible.
- KPI chips do not horizontally scroll.
- Table horizontal scrolling belongs to the grid container.
- Batch/sidebar and document pane do not overlap the template header.

## 5. App-Native Confirm Dialogs

Files changed:

- `webapp/frontend/src/components/ConfirmDialog.tsx`
- `webapp/frontend/src/App.tsx`
- `webapp/frontend/src/components/BatchActionsBar.tsx`
- `webapp/frontend/src/styles.css`

Replaced native confirms for:

- Stop processing.
- Delete batch.
- Reprocess with unsaved edits.
- Refresh preview with unsaved edits.

Verification:

```powershell
Select-String -Path "webapp\frontend\src\**\*.tsx","webapp\frontend\src\*.tsx" -Pattern "window.confirm"
```

Result: no matches.

## 6. Safe QA Fixture Seed

Added:

`scripts/seed_webapp_qa_fixture.py`

Added npm script:

```powershell
cd webapp/frontend
npm.cmd run seed:qa-fixture
```

The script creates or reuses a batch named `QA Visual Fixture` with:

- 1 placeholder `.txt` file.
- 3 invoices.
- 6 preview rows.
- 1 manual-review item.
- A processed `_webapp_result.json` cache.

It does not:

- Run vendor processors.
- Use Dropbox.
- Use AI.
- Use OCR.
- Read or modify source bills.
- Modify `Output/Template.xlsx`.

Fixture seeded in this phase:

`batch_20260502_185801_507`

## 7. Required Verification

Passed:

```powershell
cd webapp/frontend
npm.cmd run build
npm.cmd run test:e2e
```

Passed:

```powershell
python -m compileall webapp\backend
python scripts\verify_backend_routes.py
python -m py_compile scripts\seed_webapp_qa_fixture.py
```

Integrity check:

```powershell
git status --short -- Output\Template.xlsx "Training Bills_Invoices" .env .env.example
```

Result: no changes.

## 8. Files Changed

Phase 1T files:

- `webapp/frontend/package.json`
- `webapp/frontend/package-lock.json`
- `webapp/frontend/playwright.config.ts`
- `webapp/frontend/e2e/operator-visual.spec.ts`
- `webapp/frontend/src/components/ConfirmDialog.tsx`
- `webapp/frontend/src/components/BatchActionsBar.tsx`
- `webapp/frontend/src/App.tsx`
- `webapp/frontend/src/styles.css`
- `scripts/seed_webapp_qa_fixture.py`
- `docs/reports/phases/screenshots/phase_1t_responsive/*`
- `docs/reports/phases/WEBAPP_PHASE_1T_BROWSER_AUTOMATION_AND_CONFIRM_MODALS_REPORT.md`

Some other frontend files are already dirty from earlier phases; they were not reverted.

## 9. Known Limitations

- The safe QA fixture uses a placeholder `.txt` document, so it verifies layout without real bill data or Dropbox side effects. It does not exercise PDF marking/region drawing.
- npm audit vulnerabilities remain for a future dependency hygiene pass.
- Full Richmond/Hopkinsville processing was not run in this phase to avoid Dropbox and source/output side effects.

## 10. Next Recommended Phase

Phase 1U should focus on PDF/document workspace browser coverage:

1. Add a generated safe local PDF fixture for visual QA.
2. Extend Playwright coverage to document pane page controls, zoom, mark tool, and region persistence.
3. Add a no-Dropbox processor fixture mode if full process/cancel UI needs browser automation without external uploads.
