# Webapp Phase 1R Full Operator QA Sweep + Immediate UI Bug Fixes Report

Date: 2026-05-02

## 1. QA Scope

Phase 1R reviewed the Billing Refactoring 2026 Web Console as an operator-facing workspace. The pass covered:

- Clean stack verification on backend `http://localhost:8001` and frontend `http://localhost:5174`.
- Backend route contract and live OpenAPI verification.
- Source-level UI surface inventory.
- Endpoint-driven operator smoke tests for batch lifecycle, upload/preview, unsupported-only processing, regions, seeded template preview, manual review, and edited export.
- Safe immediate fixes for UI integration bugs found during the sweep.

Browser automation packages were checked through the Node REPL and were not available:

```json
{
  "playwright": "unavailable",
  "puppeteer": "unavailable",
  "selenium-webdriver": "unavailable"
}
```

Because of that, click-level browser interactions were reviewed from source and through API-backed smoke tests. Manual browser verification is still recommended for final visual approval.

## 2. Clean Stack Verification

Observed clean stack:

- Backend PID 47140: `python -m uvicorn webapp.backend.main:app --reload --port 8001`
- Frontend PID 50080: `vite --port 5174`
- `http://localhost:5174/api/health` successfully proxied to the backend.

Live OpenAPI on port 8001 includes:

| Route | Method | Present |
| --- | --- | --- |
| `/api/batches/{batch_id}` | PATCH | yes |
| `/api/batches/{batch_id}/cancel` | POST | yes |
| `/api/batches/{batch_id}/regions` | GET | yes |
| `/api/batches/{batch_id}/regions` | PUT | yes |
| `/api/ai/status` | GET | yes |

Port 8000 still has listeners and was not used for this phase.

## 3. Surfaces Tested / Inventoried

| Surface | Visible UI / flow reviewed |
| --- | --- |
| Topbar | brand, workflow strip, issues pill, AI status pill |
| Left nav rail | Batches active item, Review/Vendors/Exports/Settings disabled states, no visible Soon badges |
| Batch/file sidebar | batch header, dropdown, new batch modal, rename modal, delete flow, document mode, upload/dropzone, file cards, process/stop, More menu |
| Document workspace | empty state, PDF preview path, PDF.js workspace, page nav, zoom, select/draw/pan/delete tools, field label selector, collapse rail |
| Template workspace | summary stats, Required/Issues/All column views, search, filters, editable grid, selected row, support link chip, export action |
| Issues drawer | open/close source, empty/issues states, issue cards, show row, open document, mark reviewed |
| Toasts | success/warning/error text paths, auto-dismiss component, no raw JSON from normalized API errors |
| Modals/dialogs | new batch, rename batch, remaining temporary browser confirms for stop/delete/discard edits |
| AI popover | AI Off copy, provider/status rows, collapsed developer setup, disabled Configure AI |
| Loading states | batch switch toast, document loading copy, PDF render loading, processing/progress, export, region save/load |

## 4. Bugs Found

| Bug | Impact | Evidence | Status |
| --- | --- | --- | --- |
| All column view could still hide optional columns | Operator clicking All did not necessarily see every `Template.xlsx` column because the inner grid had its own optional-column toggle | `TemplateWorkspace` passed full columns, but `ResManTemplatePreview` defaulted to hidden optional columns | fixed |
| More-menu delete could double-confirm | Delete from sidebar More menu called a local confirm, then App-level delete confirm ran again | `BatchActionsBar.confirmAndClear()` plus `App.handleClear()` both prompted | fixed |
| Create batch accepted overlong names in UI/API | Long labels could become ugly in batch header/dropdown | Create path had no frontend 80-char guard and backend create did not enforce max length | fixed |
| Legacy/missing metadata used raw batch id as primary label | Old batches without metadata could show raw ids instead of friendly fallback | Backend list/get returned `batch_id` when `batch_name` missing | fixed |
| Corrupt/stale remembered batch could surface an avoidable banner | Invalid localStorage batch id was cleared but still displayed a persistent error | App restore catch only suppressed 404, not 400 invalid batch | fixed |
| Export warning for unresolved issues was weak | Operator could export flagged rows with only a tooltip warning | Template export title warning depended on `hasExport`; no toast before export | fixed |
| Document marks copy used technical region wording | Less polished operator language | Toolbar label and count said `Region` / `region(s)` | fixed |

## 5. Bugs Fixed Immediately

### Template All Columns

Files:

- `webapp/frontend/src/components/ResManTemplatePreview.tsx`
- `webapp/frontend/src/components/TemplateWorkspace.tsx`

Fix:

- Added `forceShowOptional` to the grid.
- `TemplateWorkspace` passes `forceShowOptional={view === "full"}`.
- The inner optional-column toggle is hidden when All is active so the All view means all columns.

### Delete Flow

File:

- `webapp/frontend/src/components/BatchActionsBar.tsx`

Fix:

- Removed the extra More-menu delete confirmation.
- App-level `handleClear()` remains the single confirmation owner.

### Batch Naming

Files:

- `webapp/backend/api/batches.py`
- `webapp/frontend/src/App.tsx`

Fix:

- Backend create trims supplied names and rejects names over 200 characters.
- Frontend create enforces the same operator-facing 80-character limit used by rename.
- Missing metadata now displays `Untitled batch`.

### Stale Batch Restore

File:

- `webapp/frontend/src/App.tsx`

Fix:

- 400/404 restore failures now clear localStorage and show a non-blocking info toast instead of a persistent technical banner.

### Export Warning

Files:

- `webapp/frontend/src/App.tsx`
- `webapp/frontend/src/components/TemplateWorkspace.tsx`

Fix:

- Export title warns whenever flagged rows exist, not only when a previous export exists.
- Export now emits a warning toast when unresolved issues remain.

### Document Marks Copy

File:

- `webapp/frontend/src/components/pdf_workspace/ViewerToolbar.tsx`

Fix:

- Field label changed from `Region:` to `Field:`.
- Count changed from `region(s)` to `mark(s)`.
- Draw action title now says `Mark a field on the page`.

## 6. QA Checklist Results

| Surface | Interaction | Expected | Actual | Status | Fix implemented | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Backend stack | Route verifier | Critical routes registered | Passed | pass | no | `scripts/verify_backend_routes.py` |
| Live stack | Frontend `/api/health` | Vite proxies to 8001 | Passed | pass | no | Confirms frontend intended backend |
| Batch modal | Create custom name | Name persists | Passed via API | pass | yes | Long-name guard added |
| Batch modal | Create empty name | Generated readable default | Passed via API | pass | yes | Backend default `Batch YYYY-MM-DD HH:MM` |
| Batch modal | Create long name | Clean validation error | Backend 400; frontend guard | pass | yes | UI limit 80, backend max 200 |
| Batch modal | Special characters | Persist safely | Passed via API | pass | no | Special chars remained metadata only |
| Rename modal | Rename valid | Metadata updates | Passed via API | pass | no | Modal source already app-native |
| Rename modal | Empty/long rename | Friendly validation | Backend 400; UI catches normalized error | pass | no | Browser click manual recommended |
| Batch dropdown | Switch batch | Active batch updates | Source-reviewed | needs manual visual | yes | Boundary fixed in 1Q; no browser driver |
| Delete | Delete QA batch | Only QA batch removed | Passed via API | pass | yes | Double confirm fixed |
| Stale localStorage | Missing/invalid active batch | No crash/raw JSON | Source-fixed | needs manual visual | yes | Info toast path added |
| Upload | Supported PDF | File listed and previewable | Passed via API | pass | no | Used QA copy of existing PDF |
| Upload | Unsupported extension | Clean 415 | Passed via API | pass | no | UI message normalized |
| Document preview | PDF content | Inline PDF content endpoint | Passed via API | pass | no | Full canvas click QA manual |
| Regions | Empty regions | Returns 200 + empty | Passed | pass | no | |
| Regions | Save/delete | Persists and deletes | Passed | pass | no | |
| Regions | Invalid label | Clean 400 | Passed | pass | no | |
| Processing | Unsupported-only | Stages skipped, completes | Passed | pass | yes | Timeline fix from 1Q verified |
| Cancel | Idle cancel | `no_active_run` | Passed | pass | no | |
| Template | Required/Issues/All views | All means all columns | Source-fixed/build passed | pass | yes | Manual visual check recommended |
| Template | Edit/export value | Edited export path works | Passed via seeded preview/API export | pass | no | Openpyxl warned about unsupported validation extension |
| Issues drawer | Empty/issues states | No permanent width steal | Source-reviewed | needs manual visual | no | |
| AI | Status | No keys/no provider calls | Source/API-reviewed | pass | no | Only local status endpoint used |
| Error handling | Raw FastAPI JSON | Not operator-facing | Source scan clean for raw throws | pass | no | `ApiError` retained raw details for console/dev |

## 7. Backend/API Issues Fixed

- Backend batch create now validates and trims names.
- Missing batch metadata now produces a friendly `Untitled batch` label.
- Existing Phase 1Q invalid batch id protections remained intact:
  - `GET /api/batches/%2E%2E` returns 400.
  - `DELETE /api/batches/%2E%2E` returns 400.
  - valid missing delete returns 404.

## 8. Frontend Interaction Issues Fixed

- Template All columns now actually shows all columns.
- More-menu delete no longer double-confirms.
- Create batch validates long names before calling the backend.
- Stale active batch restore no longer raises an avoidable persistent banner for 400/404.
- Export with unresolved issues now warns via toast.
- Document marking copy is less technical.

## 9. Tests Performed

### Required checks

```powershell
npm.cmd run build
python -m compileall webapp\backend
python scripts\verify_backend_routes.py
```

Results: all passed.

### Live stack checks

```powershell
Invoke-RestMethod http://localhost:8001/api/health
Invoke-RestMethod http://localhost:5174/api/health
Invoke-RestMethod http://localhost:8001/openapi.json
```

Results: health passed and live OpenAPI contained required current-source routes.

### Backend/operator smoke

Covered through TestClient and live 8001:

- create named batch
- create unnamed batch
- create special-character batch
- reject overlong create
- rename batch
- reject empty/long rename
- list/get/switch-equivalent batch checks
- invalid id GET 400
- invalid id DELETE 400
- missing valid id DELETE 404
- cancel idle returns `no_active_run`
- upload PDF
- list uploaded files
- PDF preview metadata
- PDF inline content endpoint
- unsupported upload extension 415
- seeded preview
- manual-review payload
- edited export path
- regions empty/save/load/delete
- invalid region label 400
- unsupported-only processing with skipped timeline stages
- delete only QA-created batches

### CLI regression

Ran safe compile checks only:

```powershell
python -m py_compile "Training Bills_Invoices\Water - Sewer\Richmond Utilities\process_richmond_utilities.py"
python -m py_compile "Training Bills_Invoices\Water - Sewer\Hopkinsville Water Environment Authority\process_hopkinsville_water_environment_authority.py"
```

Full CLI execution was not run because those scripts can generate outputs and may trigger Dropbox side effects depending on environment credentials. That should be run only when external upload side effects are explicitly desired.

### Integrity

Checked:

```powershell
git status --short -- Output\Template.xlsx "Training Bills_Invoices" .env .env.example
```

Result: no changes reported for `Output/Template.xlsx`, source training files, or env files.

No AI provider calls were made. Dropbox was not invoked.

## 10. Screenshots / Manual Checks Recommended

Because browser automation was unavailable, run these manually on `http://localhost:5174`:

1. Open New Batch modal; test Enter, Escape, outside click, long name, custom name.
2. Open Rename modal; test Enter, Escape, outside click, empty/long validation.
3. Open batch dropdown; click rows; verify active batch changes and no dropdown flicker.
4. Upload a PDF by click and by drag/drop; drop outside the zone and verify the browser does not navigate.
5. Open PDF workspace; page nav, zoom, mark field, delete mark, collapse/expand.
6. Seed or process a preview and test Required/Issues/All column views visually.
7. Single-click rows and double-click cells; Enter/Escape edit behavior.
8. Open Issues drawer; show row, open document, mark reviewed, close with Escape/outside click.
9. Click AI pill; ensure no key material appears and popover closes cleanly.
10. Trigger a 405 or stopped backend scenario and confirm the UI shows friendly restart guidance, not raw JSON.

## 11. Known Limitations / Deferred Items

- Remaining `window.confirm(...)` calls are still browser-native for:
  - reprocess discards edits
  - refresh preview discards edits
  - stop processing
  - delete batch
- A future phase should replace those with app-native confirmation modals.
- Full supported-vendor process/cancel testing was not run to avoid Dropbox/external upload side effects.
- Browser screenshots and pixel-level visual QA were not possible without installing Playwright/Puppeteer/Selenium.
- PDF mark drawing coordinates after zoom were reviewed by source/geometry only; manual browser verification is still needed.

## 12. Next Recommended Phase

Phase 1S should focus on browser-verified polish:

1. Add a lightweight Playwright smoke suite for batch modals, dropdown switching, document marks, issues drawer, and template editing.
2. Replace remaining browser-native confirmations with app-native confirmation modals.
3. Add a QA fixture mode that seeds preview/manual-review data through a dev-only endpoint or script without vendor processing/Dropbox.
4. Run full Richmond and Hopkinsville process/cancel/export QA in an environment where Dropbox side effects are expected and safe.
