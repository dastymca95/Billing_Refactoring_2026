# Phase UI-Layout-3 - Batch Selector Header Reposition + Minimal Batch Dropdown

## 1. Header hierarchy change

The document viewer header now presents context in batch-first order:

`[Batch name] / [Current document name]`

Fallbacks are preserved:

- `Select batch / No document selected` when there is no active batch/file context.
- The document name remains readable text and is no longer the primary dropdown trigger.

## 2. Batch dropdown relocation

The batch selector trigger was moved into the left header breadcrumb area. The active batch name is the clickable control, and the document name follows it as secondary context. Existing right-side actions remain on the right side of the header:

- Process batch
- detach/reattach document viewer

## 3. Removal of page rows from dropdown

`BatchExplorer` now supports a dropdown mode through `showPages={false}`.

The header dropdown renders:

- batches
- files within expanded batches
- file badges, file name, size, status, and kebab actions

It no longer renders:

- page rows
- page list buttons
- file page expand toggles in the dropdown

Document page navigation remains in the PDF/image viewer only.

## 4. Selection styling changes

The selected batch/file styling in the dropdown was reduced to a soft blue-gray background. The previous thick left accent stripe and inset blue bar were removed for dropdown-selected rows.

Hover states now use a neutral fill without scale, border animation, or heavy outline.

## 5. Performance optimizations

The dropdown avoids rendering page rows and avoids page-list DOM growth. Search continues to use React deferred input handling, and batch/file list rendering keeps the existing lightweight batch-windowing path for very large batch lists.

Expected impact:

- faster dropdown open
- less DOM in expanded batches
- fewer visual jumps while selecting files
- document viewer does not rerender just because page rows open inside the dropdown

## 6. Tests performed

Frontend:

- `npm.cmd run build` - passed
- `npx.cmd tsc --noEmit` - passed
- `npm.cmd run test:e2e` - passed: 36 passed, 1 skipped

Backend:

- `python -m compileall webapp\backend` - passed
- `python scripts\verify_backend_routes.py` - passed

Browser QA:

- Header showed `henderson / UtilityBill - 2026-05-27T104521.716.pdf`.
- Clicking the batch name opened the dropdown.
- Dropdown showed batches and files only.
- Dropdown contained 0 page rows and 0 page lists.
- Selecting a file updated the document viewer.
- Document viewer page controls remained visible.
- Selected row styling had no left border and no inset stripe.

## 7. Screenshots

Screenshots were captured here:

`docs/reports/phases/screenshots/phase_ui_layout_3_batch_dropdown_header/`

Files:

- `header_dropdown_batches_files_only.png`
- `header_breadcrumb_page_controls.png`

## 8. Limitations

The dropdown search remains intentionally light: it filters batch metadata available in the batch list and does not fetch heavy file/page metadata just to search. A deeper file-name index can be added later using only already-loaded file lists.

## 9. Next recommended phase

If needed, add a focused dropdown search pass that indexes already-loaded file names across expanded batches while still avoiding page metadata and full batch preloading.
