# Phase UI-Layout-1 - Template-Centered Workspace With Batch Dropdown and Right Document Viewer

Date: 2026-05-15

## 1. Layout Changes

Implemented a template-centered workspace:

- Left rail remains compact and blue.
- Template is now the dominant center workspace.
- Document Viewer is positioned on the right.
- The persistent Batches workspace column is no longer rendered.
- The layout uses rounded white workspace panels on a soft gray shell background.

No backend processing logic was changed.

## 2. Batch Dropdown Behavior

Added `BatchSelectorDropdown` as a compact control in the Document Viewer header.

The dropdown reuses the existing `BatchExplorer` behavior, so these workflows remain available:

- switch batch
- create new batch
- rename batch
- delete batch
- process batch
- process file
- add/upload files
- expand batch files/pages
- select file/page and update the Document Viewer

The dropdown is searchable, scrollable, dismisses on outside click/Escape, and keeps nested kebab menus working.

## 3. Document Viewer Relocation

The existing `DocumentPreviewPanel` is now visually on the right side of the main workspace.

Preserved:

- PDF/image preview
- zoom/pan controls
- page navigation
- trace overlays
- AI scan overlay
- vision overlay hooks
- selected file/page synchronization

## 4. Responsive Behavior

Desktop:

- left rail
- center Template
- right Document Viewer
- batch dropdown anchored from the Document Viewer header

Medium/small:

- Template remains primary.
- Document Viewer collapses at narrow widths.
- Batch dropdown becomes full-width within safe margins.

## 5. Components Modified

Frontend:

- `webapp/frontend/src/App.tsx`
- `webapp/frontend/src/components/BatchExplorer.tsx`
- `webapp/frontend/src/components/BatchSelectorDropdown.tsx`
- `webapp/frontend/src/components/TemplateWorkspace.tsx`
- `webapp/frontend/src/components/WindowsMenu.tsx`
- `webapp/frontend/src/styles.css`

E2E tests updated:

- `webapp/frontend/e2e/operator-visual.spec.ts`
- `webapp/frontend/e2e/utility-u4.spec.ts`
- `webapp/frontend/e2e/ingestion-ai9.spec.ts`

## 6. Tests Performed

Frontend:

- `npm.cmd run build` - PASS
- `npx.cmd tsc --noEmit` - PASS
- `npm.cmd run test:e2e` - PASS, 34 passed, 2 skipped

Backend / smoke:

- `python -m compileall webapp\backend` - PASS
- `python scripts\verify_backend_routes.py` - PASS
- `python scripts\smoke_document_ingestion.py` - PASS
- `python scripts\smoke_utility_processors.py` - PASS
- `python scripts\smoke_canonical_invoice_fixtures.py` - PASS

## 7. Screenshots Path

Screenshots captured at:

`docs/reports/phases/screenshots/phase_ui_layout_1_template_centered_workspace/`

Files:

- `normal_layout.png`
- `batch_dropdown_open.png`
- `single_invoice_mode.png`

## 8. Limitations

- The batch dropdown intentionally reuses `BatchExplorer`, so its content remains feature-rich rather than a minimal command palette.
- The legacy Window menu now controls Template and Document Viewer only; Batches are accessed from the Document Viewer header selector.
- The right Document Viewer collapses on smaller screens instead of becoming a fully separate mobile drawer.

## 9. Next Recommended Phase

Polish the batch dropdown interaction density:

- tighter batch/file row typography
- optional keyboard-first command palette behavior
- compact file upload progress inside dropdown
- right-panel collapse toggle in the Template header
