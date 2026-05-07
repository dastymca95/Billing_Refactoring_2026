# Phase 2C — Template Workspace Pro Mode

**Date:** 2026-05-03
**Scope:** Major UX upgrade for the ResMan/template workspace: contextual breadcrumb header, modernised KPI row, electric-blue required column headers, removal of the orange row stripe, in-app focus mode, popout windows for multi-monitor work, and an editable export workbook name.

---

## 1. Template header redesign

The workspace title is no longer the generic "ResMan Template". It is now a three-line breadcrumb header that tells the operator exactly where they are.

```
Batches › Richmond 3 › ResMan Import Template       [⊙] [⤴] [⤢] [Export]
Richmond_3_Import.xlsx                              ✏︎
Richmond Utilities · 📄 Richmond Utilities - Blue Country 4-6-26.pdf · Page 1 of 14
```

- **Line 1 — breadcrumb.** Static "Batches" → batch name → "ResMan Import Template". Hover-light style, monochrome separators.
- **Line 2 — main title.** The export workbook display name (default `<batch_name>.xlsx`, editable in place — see Section 9).
- **Line 3 — context.** Vendor label (`Richmond Utilities`, `Hopkinsville Water`, or `Mixed vendors`), selected document, and "Page x of y" when applicable.

Right-aligned action cluster: **issue badge → popout document → popout template → focus toggle → Export**.

Implementation: [TemplateWorkspace.tsx](webapp/frontend/src/components/TemplateWorkspace.tsx) — new `Props.batchName / vendorLabel / exportName / defaultExportName / activeDocumentPageCount / focusMode / onPopout* / onToggleFocusMode / onRenameExport`. The unused `titleMeta` from Phase 1J was retired.

## 2. Breadcrumb / context model

`App.tsx` now keeps the relevant context centralised:
- `batchName` — already existed; passed through.
- `vendorLabel` — derived by `deriveVendorLabel(status)` from the per-batch detection summary; resolves to `"Richmond Utilities"`, `"Hopkinsville Water"`, `"Mixed vendors"`, or empty string.
- `activeDocumentPage` — already existed (filename + page).
- `activeDocumentPageCount` — derived from `preview.rows[*]._meta.source_page` (max page number observed for the active filename). Falls back to `null` when not derivable; the UI then renders `"Page 1"` without `"of N"`.

## 3. KPI redesign

`.summary-stat` was converted from a heavy bordered chip to a calm inline row:
- No borders, no backgrounds.
- Stats separated by a faint `·` glyph (CSS-generated `::before`).
- `tone-strong` (Total) gets a slightly larger, tighter-tracked treatment.
- `tone-warn` only kicks in when the value > 0 (otherwise the stat reads neutral).
- Labels normal-case, 11px; values 12px, semibold, tabular-nums.

```
Files 1 · Invoices 14 · Rows 16 · Issues 16 · Edited 0 · Missing link 16 · Total $1,479.83
```

The stat order is unchanged (Files, Invoices, Rows, Issues, Edited, Missing link, Total).

## 4. Required column header redesign

| Before | After |
|---|---|
| `background: #ffd6a8 (orange); color: #7a3a00; border-bottom: 2px solid #d97706` | `background: var(--accent) (electric blue); color: #ffffff; border-bottom: 2px solid var(--accent); font-weight: 700` |
| Recommended: `background: #fff8c5 (amber); color: #6b5300` | Recommended: `background: rgba(37, 99, 235, 0.10); color: #1e3a8a; border-bottom: 2px solid rgba(37, 99, 235, 0.45)` |
| Optional: muted text on default header bg | Optional: white background, `var(--text)`, thin border-bottom |

Both occurrences of the orange rule (lines 670 & 2525 of `styles.css`) were rewritten so nothing later in the cascade resurrects the pastel.

The `*` marker glyph on required headers now renders white on blue; on recommended headers it picks up the accent colour.

## 5. Row-stripe removal

The orange `box-shadow: inset 3px 0 0 var(--warning)` on `tr.review-row td:first-child` is gone. Review rows now carry a uniform soft warm tint (`rgba(245, 158, 11, 0.04)`) across the row. Selection still wins (selected rows always get the blue accent-soft background), and the `.review-row` class itself is preserved so the issues drawer still finds them.

## 6. Focus / fullscreen mode

Implemented as **app-level focus**, not browser fullscreen.

- New state in `App.tsx`: `focusModeTemplate`.
- New layout class on `.layout`: `focus-mode-template`. CSS hides the file sidebar, the document pane, and the resizers; `<main className="template-and-inspector">` flexes to 100% width.
- Toggle from the template header (expand/contract icon) or from the **"Focus mode · Exit (Esc)"** banner shown inline.
- Escape key listener in App.tsx exits focus mode, but only when no input/textarea/contenteditable owns the keystroke (so typing inside cells / search / rename input still works).
- Sidebar/document widths are not modified — they're reset by CSS only — so `useResizablePanel` restores them automatically when focus is exited.

## 7. Resizable panels — audit

The existing `hooks/useResizablePanel.ts` is solid:
- Pointer capture is set on the divider so all subsequent move/up events route to it (kills the "sticky resize" bug seen pre-1L).
- A `e.buttons === 0` hard guard fires on every move, so OS-swallowed pointerups still stop the drag.
- Window-level `pointerup`, `pointercancel`, `blur`, `visibilitychange`, and `mouseleave` listeners are belt-and-braces.
- Cleanup on unmount restores `body.style.cursor` and `userSelect`.

No code change was needed in Phase 2C; the audit confirmed the API + bug fixes are still working. Sidebar and document-pane resizers continue to enforce min/max from `useResizablePanel`'s config (`min: 220 / 320`, `max: 460 / 720`).

## 8. Popout windows

Implemented as a **hash-based route** so we don't have to introduce react-router for two screens.

- Routes:
  - `#popout/template?batch=<id>` → read-only TemplateWorkspace
  - `#popout/document?batch=<id>&file=<filename>` → DocumentPreviewPanel
- Open via `window.open(url, name, "popup=yes,…")` from the template header buttons.
- `main.tsx` was upgraded to a thin `RootRouter` that listens to `hashchange`. When the hash matches a popout route, it renders [PopoutPage.tsx](webapp/frontend/src/components/PopoutPage.tsx) and the body gains a `popout-mode` class. Otherwise it renders the normal App.
- Popouts call the same backend APIs (`api.getBatch`, `api.preview`, `api.manualReview`) — no shared state, no two-window editing logic.

**Read-only by design.** Cell edits in the main app live in `App.tsx` state and are persisted only at Export time. A popout that pretended to edit would either lose the edits or contradict the host window. The popout therefore renders TemplateWorkspace with `readOnly={true}`, which:
- hides the export button, focus toggle, popout buttons, and the rename pencil
- shows a `Read-only` pill in the popout's header bar
- disables the export-name field (clickable text is shown without the editor)

Closing the popout uses the standard "Close window" link in the popout header (calls `window.close()`).

**Editable two-window sync** is explicitly deferred to Phase 2D — when (if) edits are persisted server-side.

## 9. Export / template naming

Backend ([webapp/backend/api/batches.py](webapp/backend/api/batches.py)):
- `UpdateBatchBody.export_name: Optional[str]` accepted by `PATCH /api/batches/{batch_id}`.
- Sanitiser `_sanitize_export_name`:
  - Strips path components via `Path(value).name`.
  - Replaces `\ / : * ? " < > |` with `_`.
  - Strips leading/trailing `.` and spaces.
  - Rejects blanks (`empty` after strip → 400).
  - Caps length at 120 (→ 400).
  - Forces `.xlsx` extension by replacing whatever extension the operator supplied.
- Stored in `batch_metadata.json` under `export_name`. Read back by `GET /api/batches/{id}` so the UI rehydrates after a refresh.

Frontend:
- `api.updateBatch({ exportName })` shape extended; sends `export_name` in the body.
- Template header surfaces it via the `<ExportNameField>` widget — click to edit, Enter saves, Esc cancels, blur saves. Errors snap the field back to the canonical value.
- The default fallback is `${batchName}.xlsx` (with the same illegal-char strip) when no rename has been saved.

**Limitation:** the backend's `/export` download still names files via the vendor processor's own naming convention (e.g. `richmond_utilities_resman_import_<timestamp>.xlsx`). Phase 2C plumbs the display name into the metadata + UI but does not yet rewire the download filename — the operator's display name and the actual download name can diverge until Phase 2D. The metadata is read-only for the CLI, so no CLI behaviour changes.

## 10. Screenshots

Required screenshots live under
[`docs/reports/phases/screenshots/phase_2c_template_workspace_pro/`](docs/reports/phases/screenshots/phase_2c_template_workspace_pro/).

The directory was created and the dev stack is running (frontend 5174 / backend 8001) but the local Chrome extension that drives automated capture was offline during this report run. The user should grab the following manually (or rerun the screenshot pass once the extension reconnects):

1. `01_template_normal.png` — default template workspace (breadcrumb + KPI row + table).
2. `02_required_headers_blue.png` — close-up of the required column headers in electric blue.
3. `03_focus_mode.png` — template after pressing the focus button (sidebar + document pane hidden).
4. `04_popout_template.png` — second window opened via the popout button, showing the read-only header.
5. `05_layout_restored.png` — same template after exiting focus mode, sidebar/document widths restored.
6. `06_row_selected_and_review.png` — a row selected on a `.review-row` to confirm the new tint replaces the orange stripe.

## 11. Tests performed

| Test | Result |
|---|---|
| `cd webapp/frontend && npm run build` | ✓ 68 modules, 263.81 kB JS / 93.76 kB CSS |
| `python -m compileall webapp/backend -q` | ✓ no errors |
| `npx tsc --noEmit` (incremental during build) | ✓ no type errors |
| Backend smoke — `export_name` save / load round-trip | `Richmond 3 Import` → `Richmond 3 Import.xlsx` saved + read back |
| Backend smoke — sanitisation: `../../escape` | → `escape.xlsx` |
| Backend smoke — sanitisation: `foo<>:|*?".bar` | → `foo_.xlsx` |
| Backend smoke — sanitisation: `whatever.csv` | → `whatever.xlsx` (extension swap) |
| Backend smoke — empty `export_name` | 400 "export_name cannot be empty" |
| Backend smoke — `'a' * 200` | 400 "export_name too long (max 120)" |

**Integrity invariants** — checked before and after the report run:
- `Output/Template.xlsx` SHA-256 unchanged: `b753f406c0222f15`.
- `Vendors/Vendor List.csv` SHA-256 unchanged: `7839a43a493a7c0c`.
- `config/vendors/hopkinsville_water_environment_authority.yaml` unchanged: `e83c554709edd0bf`.
- `config/vendors/richmond_utilities.yaml` unchanged: `6111d042658818d4`.
- `config/vendors/backups/` empty (no save was triggered).
- No AI calls, no Dropbox calls (Phase 2C touches no processor side-effects).

`npm run test:e2e` was not executed in this report — the existing Playwright suite predates Phase 2C and would need new specs for the breadcrumb/focus/popout flows. Recommended for Phase 2D.

## 12. Files touched

- `webapp/backend/api/batches.py` — `_sanitize_export_name`, `_EXPORT_NAME_ILLEGAL`, `UpdateBatchBody.export_name`, PATCH wiring; added `import re`.
- `webapp/frontend/src/api.ts` — `updateBatch({ exportName })`.
- `webapp/frontend/src/main.tsx` — `RootRouter` (hash → App | PopoutPage).
- `webapp/frontend/src/components/PopoutPage.tsx` — new file, popout host.
- `webapp/frontend/src/components/TemplateWorkspace.tsx` — breadcrumb header, action cluster (focus / popout / export), `<ExportNameField>`, new icons (Expand/Contract/Popout/PopoutTemplate/DocumentMini/Pencil), new Props.
- `webapp/frontend/src/App.tsx` — `focusModeTemplate`, `exportName`, `vendorLabel`, `deriveVendorLabel`, `prettyVendor`, `handleRenameExport`, `openPopout` / `handlePopoutTemplate` / `handlePopoutDocument`, Escape key listener, layout class `focus-mode-template`, prop wiring.
- `webapp/frontend/src/styles.css` — required-column blue, recommended-column tinted blue, optional-column white, removal of orange row stripe (warm row tint instead), modernised `.summary-stat`, full Phase 2C block (breadcrumb, title row, header actions, focus-mode layout, popout chrome).

## 13. Remaining limitations

- **Download filename still vendor-processor-driven.** `export_name` is a display field. Phase 2D should plumb it through the export endpoint's `Content-Disposition` so downloads land with the operator's name.
- **Popouts are read-only.** Two-window edit sync requires a backend edit-persistence layer that doesn't exist yet.
- **No backup browser** for export-name history (Phase 1Z's pattern could be reused if needed).
- **`activeDocumentPageCount` is derived from preview rows.** A document with pages that never produced a row (e.g. a totally OCR-failed page) won't contribute to the max. Acceptable for v1; a real `pages_total` should come from the per-file preview metadata in a future phase.
- **e2e tests not authored.** The Playwright suite needs new specs for breadcrumb visibility, the focus toggle, the popout open path, and the export-name editor.
- **Screenshots not auto-captured.** The Chrome extension was offline during this report run; capture is documented manually under `docs/reports/phases/screenshots/phase_2c_template_workspace_pro/`.

## 14. Recommended next phase

Phase 2D — Template Workspace Multi-Window Sync + Export Filename Plumbing:
1. Server-side persistence of cell edits so popouts can edit without diverging from the host window.
2. Plumb `export_name` into `/api/batches/{id}/download` so the file lands with the operator's name.
3. Backup history for export-name changes.
4. Playwright e2e specs covering the new header, focus mode, and popout flows.
5. Optional: a "Pop out batches" route for the file/sidebar manager so operators on a 2-monitor setup can keep batch selection on one screen and the template on the other.
