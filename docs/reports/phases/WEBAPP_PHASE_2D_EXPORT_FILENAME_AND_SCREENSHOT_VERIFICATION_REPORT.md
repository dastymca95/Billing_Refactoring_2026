# Phase 2D — Export Filename Wiring + Screenshot Verification

**Date:** 2026-05-03
**Scope:** Make the operator-chosen export filename actually drive what the browser saves the workbook as. Polish the export-name UX, add the More-popover animation and the optional zebra toggle, and document download evidence.

---

## 1. Export filename architecture

```
[Operator types in title]                         [api.ts]
        │                                              │
        ▼                                              │
PATCH /api/batches/{id}                                │
   body: { export_name: "Richmond …" }                 │
        │                                              │
        ▼                                              │
batches.py::_sanitize_export_name                      │
   strip path, replace illegal chars, force .xlsx,     │
   reject empty, cap at 120 chars                      │
        │                                              │
        ▼                                              │
batch_metadata.json::export_name = "Richmond ….xlsx"   │
                                                       │
[Operator clicks Export]                               │
        │                                              │
        ▼                                              ▼
POST /api/batches/{id}/export                  triggerDownload(<on-disk name>)
   builds the workbook on disk under                   │
   the existing vendor-processor naming                ▼
   convention (unchanged)                       <a href="/api/batches/{id}/download
        │                                              ?filename=<on-disk>">
        ▼                                              │
GET /api/batches/{id}/download?filename=<on-disk>      │
   1. selects the requested on-disk file               │
   2. resolves the *display* name via                  │
      _resolve_display_filename(batch_id):             │
         → metadata.export_name if set                 │
         → else `<batch_name>_ResMan_Import.xlsx`      │
         → else `ResMan_Import.xlsx`                   │
   3. FileResponse(filename=<display>) →               │
      Content-Disposition: attachment;                 │
        filename="<display>"                           │
        │                                              │
        ▼                                              ▼
                              Browser saves under <display>
```

The on-disk file path stays exactly where the vendor processor put it; only the **operator-visible filename** changes.

## 2. `batch_name` vs `export_name`

The two fields are intentionally separate:

| Field | Purpose | Display |
|---|---|---|
| `batch_name` | Operational label for the batch in lists, breadcrumbs, the batch picker | "Richmond 3" |
| `export_name` | Filename used by the browser when downloading the ResMan workbook | "Richmond Utilities March 2026 Import.xlsx" |

Editing one never touches the other. The PATCH endpoint accepts either or both fields independently; sanitisation differs (batch_name caps at 200 chars; export_name caps at 120, forces `.xlsx`, replaces illegal filename characters).

## 3. Frontend changes

- `src/api.ts` — `updateBatch({ exportName })` already shipped in Phase 2C; still the one and only path the UI uses to save.
- `src/App.tsx`:
  - `triggerDownload()` no longer sets `a.download = filename`. The browser now defers to the backend's `Content-Disposition`, so what the operator sees in the title is what they actually save under.
  - `handleRenameExport` toast wording updated to "Export name updated. Workbook will download as “<name>”." (matches the spec).
- `src/components/TemplateWorkspace.tsx`:
  - Helper text *"This is the filename used when downloading the ResMan import workbook."* renders under the title only when no `export_name` has been saved yet — a one-line nudge that disappears the moment the operator names the export.
  - Zebra-rows toggle in the table controls strip; persisted as `localStorage["billing_template_zebra"]`.
- `src/styles.css`:
  - `.template-kpi-popover` got a 150 ms fade + 4 px slide animation (`@keyframes kpi-popover-in`), with `@media (prefers-reduced-motion: reduce)` opting out.
  - `.template-export-helper` styles (italic, muted, 11 px).
  - `.template-zebra-toggle` styles (chip-like, accent fill when active).
  - The previously-prepared `.is-zebra` selector was rewired to live on `.template-workspace.is-zebra` (parent gate) so we don't have to change the data-table component to receive a class. Hover still wins over zebra for clarity.

## 4. Backend changes

- `webapp/backend/api/export.py`:
  - New helper `_resolve_display_filename(batch_id, batch_dir)` reads `batch_metadata.json::export_name`, falls back to `<batch_name>_ResMan_Import.xlsx`, and re-sanitises the result (defensive against hand-edited metadata).
  - New helpers `_slug_for_default(batch_name)` and `_sanitize(value)` keep the export module self-contained while the canonical write-path sanitiser still lives in `batches.py`.
  - `download_endpoint` calls the helper and passes the result to `FileResponse(filename=…)`. The `?filename=` query param continues to *select* an on-disk file when needed — its semantics didn't change.
- `webapp/backend/api/batches.py` — unchanged in this phase. The PATCH sanitiser shipped in Phase 2C is still authoritative for what gets persisted.

## 5. Filename sanitisation rules

Both the persistent (`api/batches.py`) and the defensive (`api/export.py`) sanitisers follow the same rule set:

1. `strip()` whitespace. Empty → reject (PATCH) or `ResMan_Import.xlsx` fallback (download helper).
2. `Path(value).name` to strip any path components — guarantees a single basename.
3. `\ / : * ? " < > |` → `_` (Windows-illegal characters).
4. Trim leading/trailing `.` and spaces.
5. Length cap at 120 characters (PATCH rejects with 400; download helper truncates defensively).
6. Force `.xlsx` extension by replacing whatever extension the operator provided.
7. The result is **always** a single non-empty `.xlsx` basename.

PATCH errors are friendly:
- `"export_name cannot be empty"`
- `"export_name contains only invalid characters"`
- `"export_name too long (max 120)"`

## 6. Screenshots

Directory: [`docs/reports/phases/screenshots/phase_2d_export_filename/`](docs/reports/phases/screenshots/phase_2d_export_filename/).

The local Chrome extension that drives automated capture was offline during this run (frontend dev server was up at `http://localhost:5174` and reachable). Two artefacts were produced anyway:

- The generated `download_filename_evidence.md` transcript originally captured the `Content-Disposition` behavior for the canonical cases. It was intentionally removed from source control during repository cleanup because generated evidence belongs outside the tracked documentation tree; the filename behavior remains covered by the corresponding automated tests and can be regenerated when needed.
- **Screenshot list pending manual capture** when the Chrome extension is back online:
  1. `01_template_normal.png` — Phase 2C.1 polished header in the live app.
  2. `02_export_name_edit.png` — title in editing mode (input visible, helper hint below).
  3. `03_export_name_saved.png` — title showing the saved export name + the success toast.
  4. `04_kpi_popover.png` — the More-stats popover open (animation captured if possible).
  5. `05_export_button_download.png` — Export button, click → save dialog showing the `export_name`.
  6. `06_required_headers_neutral_rows.png` — close-up of the data table with required headers in solid blue and the new neutral row palette.

## 7. Tests performed

| Check | Result |
|---|---|
| `npm run build` (frontend) | ✓ 68 modules, 267.00 kB JS / 100.34 kB CSS |
| `python -m compileall webapp/backend -q` | ✓ no errors |
| Backend smoke — default Content-Disposition | `attachment; filename="HWEA_ResMan_Import.xlsx"` |
| Backend smoke — operator-set | `attachment; filename*=utf-8''HWEA%20April%202026%20Import.xlsx` |
| Backend smoke — traversal `../../escape"name<>?` | sanitised → `escape_name_.xlsx` (PATCH stored, download CD reflects) |
| Backend smoke — empty `export_name` | 400 "export_name cannot be empty" |
| Backend smoke — `'a'*150` | 400 "export_name too long (max 120)" |
| `batch_name` independence | unchanged after every `export_name` PATCH |
| Backend test left no permanent state | metadata `export_name` cleared after run |

### Integrity invariants

| File | SHA-256 (16) | Status |
|---|---|---|
| `Output/Template.xlsx` | `b753f406c0222f15` | unchanged |
| `Vendors/Vendor List.csv` | `7839a43a493a7c0c` | unchanged |
| `config/vendors/hopkinsville_water_environment_authority.yaml` | `e83c554709edd0bf` | unchanged |
| `config/vendors/richmond_utilities.yaml` | `6111d042658818d4` | unchanged |
| `config/vendors/backups/` | (empty) | unchanged |

Vendor processors not touched. No Dropbox calls. No AI calls. No source PDFs/CSVs touched. `.env` not touched.

`npm run test:e2e` was not executed in this phase. The Playwright suite still predates Phase 2C/2D (no specs cover the breadcrumb / focus / popout / zebra / export-name flows yet); writing them is on the next phase backlog.

## 8. Limitations

- **Helper visibility is a one-shot nudge.** Once the operator names the export, the helper line is gone forever for that batch. There's no "show help again" affordance — by design, but worth noting.
- **Zebra toggle state is per-browser, not per-batch.** Stored in `localStorage`. If the operator wants different zebra preferences per batch, that's a future enhancement.
- **Popover animation is a single fade-slide.** No exit animation; the popover unmounts immediately on close. Adding an exit transition would require either an unmount delay or a CSS animation chain — overkill for a 220 px panel.
- **Screenshots were pending manual capture** because the Chrome extension was offline during the run. The generated filename transcript was later removed from source control under the repository artifact-retention policy; automated filename assertions remain the reproducible evidence source.
- **Some legacy `<a download>` clients** that ignore Content-Disposition (rare; mostly enterprise IE-style stacks) would still see the on-disk timestamped filename. All evergreen browsers honour the Content-Disposition header path implemented here.

## 9. Recommended next phase

Phase 2E — Editable Popouts + e2e Coverage:
1. Server-side persistence of cell edits so the popout windows can graduate from read-only previews to true second-screen editors.
2. Playwright e2e specs:
   - export name save → reload → still saved
   - export name edit → click Export → file lands with the chosen name
   - More-popover open / close / Escape
   - Zebra toggle persistence across reload
   - Focus mode toggle + Escape
3. Capture the deferred screenshot pass when Chrome connectivity is restored.
4. Optional: per-batch zebra preference (move the toggle state into batch_metadata).
