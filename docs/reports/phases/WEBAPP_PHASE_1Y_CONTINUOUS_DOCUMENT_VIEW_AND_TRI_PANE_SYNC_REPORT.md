# Phase 1Y - Continuous Document View And Tri-Pane Sync Report

Date: 2026-05-03  
App: Billing Refactoring 2026 Web Console  
Clean stack used: Backend `http://localhost:8001`, Frontend `http://localhost:5174`

## Scope

Implemented a synchronized document-navigation upgrade across:

- Left batch/file/page navigator
- Center PDF document viewer
- Right ResMan template table

Vendor extraction logic, Dropbox, AI activation, export workbook writing, `Output/Template.xlsx`, `.env`, and source PDFs/CSVs were not modified.

## Root Cause / Current Limitations

The previous document workspace was page-by-page. The selected file lived in the sidebar, the active page lived privately inside `PdfWorkspace`, and template rows only knew their template values. There was no shared navigation state, no page tree in the batch manager, and preview rows did not expose stable source-file/source-page metadata to the frontend.

The backend cached result did already include invoice-level `source_file` for current processed batches. Some processors can carry page-level detail internally, but older web cache JSON did not expose page number on flattened preview rows. Phase 1Y adds a safe backend preview-enrichment layer so the frontend receives a real API contract for source file/page linkage without changing vendor processor behavior.

## New Interaction Model

The app now has shared document navigation state:

- Clicking a file opens it and targets page 1.
- Expanding a PDF file in the batch explorer shows `Page 1`, `Page 2`, etc.
- Clicking a page row scrolls the center viewer to that page.
- Manual PDF scrolling updates the active page state.
- The left page row and matching ResMan template rows update from that active page.
- Clicking a template row navigates back to its source file/page when row metadata is available.

Highlighting is intentionally subtle:

- Active page tree row uses a faint accent wash and small dot.
- Current document page rows in the template use a pale row tint and a narrow left-edge marker.
- The PDF active/focused page gets a light outline/shadow, not a loud block.

## Backend Linkage Fields Added

`GET /api/batches/{batch_id}/preview` now enriches each flattened row `_meta` with:

- `source_file`
- `source_page`
- `invoice_group_id`
- `invoice_number`
- `invoice_index`
- `invoice_row_index`
- `row_index`

Strategy:

- Prefer explicit invoice/debug page metadata when present.
- Otherwise, use a deterministic backend fallback:
  - Single invoice for a source file -> page 1.
  - Multiple invoices for the same source file -> invoice order within that file maps to pages 1, 2, 3, etc.

`GET /api/batches/{batch_id}/files` and file preview metadata now include PDF `page_count` when available through `pypdf`. This powers the page tree without invoking vendor processors.

## Frontend Synchronization Approach

Changed files:

- `webapp/frontend/src/App.tsx`
- `webapp/frontend/src/components/BatchExplorer.tsx`
- `webapp/frontend/src/components/DocumentPreviewPanel.tsx`
- `webapp/frontend/src/components/TemplateWorkspace.tsx`
- `webapp/frontend/src/components/ResManTemplatePreview.tsx`
- `webapp/frontend/src/components/pdf_workspace/PdfWorkspace.tsx`
- `webapp/frontend/src/components/pdf_workspace/PdfPageCanvas.tsx`
- `webapp/frontend/src/types.ts`
- `webapp/frontend/src/styles.css`

Key implementation details:

- `App.tsx` now owns active document page and page navigation target state.
- `BatchExplorer` renders a second-level PDF page tree under files using `page_count`.
- `PdfWorkspace` renders a continuous vertical page stack and uses `IntersectionObserver` to publish the active visible page.
- Template rows receive `document-page-row` when their `_meta.source_file/source_page` matches the active document page.
- Template row click calls the existing row-selection path and additionally navigates to the row source document/page.
- Navigation targets replay after PDF metadata/page refs are ready, fixing the sequencing edge case where a page click could happen before page shells were mounted.

## Performance Strategy

- PDF document loading remains cached by file URL in `PdfPageCanvas`.
- Page rendering still uses the existing offscreen-canvas swap to avoid white flashes.
- Page wrappers reserve stable page space while a canvas is waiting for its first frame.
- `IntersectionObserver` avoids high-frequency scroll handlers for active-page detection.

Known limitation: Phase 1Y renders page shells for the whole PDF and lets PDF.js render pages through the existing canvas renderer. This is acceptable for observed QA files, including the 34-page HWEA fixture. Very large PDFs may still need true render virtualization or render-window eviction in a future performance phase.

## Browser Evidence

In the in-app browser at `http://localhost:5174/`:

- Switched to the multi-page HWEA QA batch.
- Expanded `HWEA UTILITIES.pdf`.
- Clicked `Page 2`.
- Observed the page row become active.
- Confirmed template rows for `data-source-page="2"` had `document-page-row`.

Desktop screenshots captured with Chromium at 1366x768:

- `docs/reports/phases/screenshots/phase_1y_continuous_document_sync/tri_pane_page_2_sync_1366.png`
- `docs/reports/phases/screenshots/phase_1y_continuous_document_sync/left_page_tree_page_2_active.png`
- `docs/reports/phases/screenshots/phase_1y_continuous_document_sync/continuous_document_page_2.png`
- `docs/reports/phases/screenshots/phase_1y_continuous_document_sync/template_rows_source_page_highlight.png`

The main screenshot shows:

- Left page tree active on Page 2.
- Center viewer showing Page 2 / 34.
- Right template rows for the page softly highlighted.

## E2E Tests Added / Updated

Updated `webapp/frontend/e2e/operator-visual.spec.ts` with:

- Continuous document viewer / page tree / template row sync test.
- Stable selectors for `explorer-file-node`, `explorer-file-page`, `pdf-continuous-scroll`, `pdf-page-shell`, and `template-row`.

The new test verifies:

- A processed PDF batch can be loaded.
- Page tree renders the expected number of PDF pages.
- Clicking Page 2 marks the page row active.
- Template rows with `data-source-page="2"` receive `document-page-row`.
- Clicking a source-linked template row keeps the page tree synchronized.

## Tests Performed

Frontend:

```powershell
cd webapp\frontend
npm.cmd run build
npm.cmd run test:e2e
```

Result:

- Build passed.
- E2E passed: 14 tests.

Backend:

```powershell
python -m compileall webapp\backend
python scripts\verify_backend_routes.py
```

Result:

- Backend compile passed.
- Route contract passed.

Live API checks:

- `GET http://localhost:8001/api/batches/batch_20260503_103827_704/files` returned PDF `page_count: 34`.
- `GET http://localhost:8001/api/batches/batch_20260503_103827_704/preview` returned row `_meta.source_file`, `_meta.source_page`, and `invoice_group_id`.

Integrity:

- `Output/Template.xlsx` unchanged.
- `Training Bills_Invoices` unchanged.
- `.env` unchanged.
- No AI calls made.
- No Dropbox uploads triggered.
- No vendor processors run during this phase.

## Known Limitations

- Existing cached results without explicit page metadata use backend deterministic fallback mapping. This is stable and server-side, but not as strong as processor-native page metadata for every vendor.
- Some two-page bills may only have rows linked to page 1 if the second page is a reverse/stub/terms page with no generated invoice rows.
- The in-app browser viewport was narrow during visual QA and hid the center document pane via existing responsive layout. Full tri-pane visual confirmation was captured with Chromium at 1366x768.
- True PDF render virtualization remains deferred for very large PDFs.

## Recommended Next Phase

Phase 1Z should harden processor-native provenance:

- Add explicit `source_page` to future vendor result caches where processors know page number.
- Consider exposing invoice/page grouping in a dedicated preview `navigation` block.
- Add render-window virtualization for PDFs above a page-count threshold.
- Add a small “Rows from this page” count near the document page context.
