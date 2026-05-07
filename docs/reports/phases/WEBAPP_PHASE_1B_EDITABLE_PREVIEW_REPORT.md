# Webapp Phase 1B — Editable ResMan Template Preview

**Date:** 2026-05-01
**Scope:** Web UI only. No changes to vendor processors, YAML rules, or `Output/Template.xlsx`.
**Result:** Operators can now click cells in the ResMan template preview, edit values inline, and export an Excel workbook that includes those edits.

---

## What was added

1. **Inline editable cells** in the preview table.
2. A **Reset Edits** button to drop all overrides.
3. A new **Export Edited Template** mode that writes a fresh xlsx from `Output/Template.xlsx` using the edited rows.
4. A confirm-before-discard prompt when re-processing or refreshing the preview while edits are unsaved.
5. A new `resman_import_edited_<TS>.xlsx` filename pattern so the legacy and edited exports can coexist in the same batch folder.

The Phase 1A behavior (process → preview → export → download with no edits) is unchanged.

---

## Files changed

### Backend

| File | Change |
| --- | --- |
| [webapp/backend/services/batch_processor.py](webapp/backend/services/batch_processor.py) | Added `_write_edited_rows_to_template(template, dest, rows)`, `_coerce_cell_value(v)`, and an `edited_rows: Optional[list[dict]]` parameter to `export_batch(...)`. When `edited_rows` is supplied, copies `Output/Template.xlsx` to `webapp_data/batches/<id>/export/resman_import_edited_<TS>.xlsx`, opens it with openpyxl, maps each row dict to template columns by exact header name, and writes the values. Keys starting with `_` (e.g. `_meta`) are skipped. The official template file is **never opened for write**. |
| [webapp/backend/api/export.py](webapp/backend/api/export.py) | Added `ExportRequest(BaseModel)` with `edited_rows: Optional[list[dict[str, Any]]] = None`. The POST `/api/batches/{id}/export` endpoint now accepts an optional JSON body and forwards it. The GET `/api/batches/{id}/download` glob was widened to `*resman_import*.xlsx` so it picks up both the legacy `<vendor>_resman_import_<TS>.xlsx` and the new `resman_import_edited_<TS>.xlsx`. |

### Frontend

| File | Change |
| --- | --- |
| [webapp/frontend/src/api.ts](webapp/frontend/src/api.ts) | `exportBatch(batchId, editedRows?)` now POSTs `{ "edited_rows": [...] }` as JSON when edits are present, otherwise sends an empty body (legacy behavior). |
| [webapp/frontend/src/types.ts](webapp/frontend/src/types.ts) | `ExportResponse` extended with optional `export_used_edited_rows`, `edited_rows_count`, and `rows_written` fields. |
| [webapp/frontend/src/components/ResManTemplatePreview.tsx](webapp/frontend/src/components/ResManTemplatePreview.tsx) | Cells are now click-to-edit. State is lifted to `App.tsx` via the `edits` prop and an `onCellEdit` callback. A controlled `<input>` replaces the `<td>` content while editing. **Enter** commits, **Escape** cancels, blur commits. Edited cells get a green background + outline; the header shows `· N cells edited`. Numeric originals (and the Amount column) are coerced back to numbers on commit; boolean originals are coerced from `"true"/"false"`. Exports a `CellEdits` type. |
| [webapp/frontend/src/components/BatchActionsPanel.tsx](webapp/frontend/src/components/BatchActionsPanel.tsx) | New props `editedCellCount` and `onResetEdits`. Adds a **Reset Edits** button. The Process button gets a tooltip warning when edits exist. The Export button label switches to `Export Edited Template (N edits)` and gains the primary style when edits are present. |
| [webapp/frontend/src/App.tsx](webapp/frontend/src/App.tsx) | Owns the `edits: CellEdits` state. Wires `handleCellEdit`, `handleResetEdits`, and an updated `handleExport` that builds the merged `editedRows` payload (every column for every row, with overrides layered on top) before calling `api.exportBatch`. `handleProcess` and `handleRefreshPreview` `window.confirm` before discarding unsaved edits. `handleClear`/`handleFiles` reset edits. |

### Untouched (intentionally)

- `Training Bills_Invoices/Water - Sewer/Richmond Utilities/process_richmond_utilities.py`
- `config/vendors/richmond_utilities.yaml`
- `Output/Template.xlsx`
- All training CSVs under `Bills_Training/`

---

## Behavior

### When the operator does **not** edit any cells

Identical to Phase 1A:

1. POST `/api/batches/{id}/export` with **no body** (or an empty body).
2. Backend copies the latest `<vendor>_resman_import_<TS>.xlsx` from `processed/<vendor>/` into `export/`.
3. Response includes `export_used_edited_rows: false`.
4. Download streams that file.

### When the operator edits one or more cells

1. Each click on a cell enters edit mode for that cell. Pressing Enter (or blurring) commits; Escape reverts.
2. The frontend tracks each override in `edits[rowIndex][columnKey]`. If the typed value equals the original, the entry is dropped — only real overrides are tracked.
3. Clicking **Export Template** triggers `handleExport`, which constructs an `editedRows` array of length `preview.rows.length`. Each entry is a dict with all 17 ResMan template column keys, populated from the original row with overrides layered on top.
4. The frontend POSTs `{ "edited_rows": [...] }` to `/api/batches/{id}/export`.
5. Backend copies `Output/Template.xlsx` to `webapp_data/batches/<id>/export/resman_import_edited_<TS>.xlsx`, opens the copy with openpyxl, reads the header row, maps each edited row dict to columns by exact header text, and writes the values into rows starting at row 2.
6. Response includes `export_used_edited_rows: true`, `edited_rows_count`, and `rows_written`.
7. Download streams the edited xlsx.

The official `Output/Template.xlsx` is read-only in this code path — it is `shutil.copy2`'d to the destination *before* openpyxl opens it for write.

### Discard guard

If the operator clicks **Process Batch** or **Refresh Preview** with `editedCellCount > 0`, a `window.confirm` dialog appears: *"Re-processing will discard N unsaved preview edit(s). Continue?"* Cancel keeps the edits intact.

---

## How to test

### 1. Build

```bash
cd webapp/frontend
npm run build
```

Expected: clean TypeScript build, ~158 KB bundle.

### 2. Run

```bash
# terminal 1
".venv/Scripts/python.exe" -m uvicorn webapp.backend.main:app --reload --port 8000

# terminal 2
cd webapp/frontend && npm run dev
```

Open http://localhost:5173.

### 3. Manual UI test (Richmond Utilities)

1. Drag the 14 CSVs from `Training Bills_Invoices/Water - Sewer/Richmond Utilities/Bills_Training/` into the drop zone.
2. Click **Process Batch**. Wait for the success banner: `Processed 14/14 files · 14 invoices · 9 flagged`.
3. The preview shows 16 rows. Verify yellow rows for flagged invoices and red cells for missing required fields.
4. Click any cell in the **Invoice Description** column. Type a new value. Press **Enter**. The cell turns green and the header shows `· 1 cells edited`.
5. Click any cell in the **Amount** column. Type `99.99`. Press **Enter**. Header shows `· 2 cells edited`. The total at the top updates accordingly.
6. Click **Export Edited Template (2 edits)**. Wait for the success banner: `Exported 1 file(s) (with 16 edited rows, 2 cells).`
7. Click **Download Excel**. Open the resulting `resman_import_edited_<TS>.xlsx`. Verify the two edited values appear in the right cells and every other row matches the original generated values.

### 4. Regression check (legacy export with no edits)

1. Click **Reset Edits**. The header drops the `cells edited` counter.
2. Click **Export Template** (note the label switched back). The success banner says `Exported 1 file(s).` (no edited-rows clause).
3. Click **Download Excel**. The downloaded file has the legacy `richmond_utilities_resman_import_<TS>.xlsx` name.

### 5. Re-process guard

1. Edit a cell.
2. Click **Process Batch**. A browser confirm dialog appears. Click **Cancel** → edits are preserved. Click **OK** → edits are discarded.

### 6. Source/template integrity

- `git status` (or file mtimes) under `Training Bills_Invoices/.../Bills_Training/`: unchanged.
- `Output/Template.xlsx`: unchanged.

---

## Smoke-test results captured during this build

Backend health, batch creation, 14-file upload, process, preview: ✓
- `summary: {files_total: 14, files_supported: 14, invoices_total: 14, manual_review_total: 9}`
- preview: 14 invoices / 16 rows.

Legacy export (no body): ✓
- response: `{export_used_edited_rows: false, exported: [...richmond_utilities_resman_import_<TS>.xlsx]}`
- downloaded xlsx: 17 rows × 24 cols, row 2 Amount = 31.42, Vendor = "Richmond Utilities".

Edited export round-trip: ✓
- POSTed 16 rows with `Invoice Description` overridden on row 0 and `Amount` overridden on row 1.
- response: `{export_used_edited_rows: true, edited_rows_count: 16, rows_written: 16}`
- downloaded xlsx: 17 rows × 24 cols.
  - row 2 `Invoice Description` = the override string ✓
  - row 3 `Amount` = `99.99` ✓
  - all other cells preserved.

Source-file integrity: ✓
- SHA-256 hashes of `Output/Template.xlsx` and all 14 Bills_Training CSVs were captured before the smoke test and re-checked after **both** the webapp round-trip *and* a fresh CLI run. All 15 files unchanged.

CLI regression check: ✓
- `python "Training Bills_Invoices/Water - Sewer/Richmond Utilities/process_richmond_utilities.py"` produced 14 invoices / 16 line items, identical structure to pre-Phase-1B baselines. Output goes to `Processed_Output/` and does not touch the inputs or `Output/Template.xlsx`.

Frontend build: ✓
- `npm run build` (tsc -b && vite build) clean, 38 modules, ~158 KB bundle.

---

## Limitations

- **Edits are not persisted.** They live in React state only. A page reload drops them, and so does re-processing or refreshing the preview (with a confirm dialog).
- **No bulk edit.** Each cell is edited individually. Find-and-replace, fill-down, multi-cell paste — none of those exist.
- **Light validation only.** The frontend coerces Amount/numeric originals to numbers and boolean originals from `true`/`false` strings. There is no required-field guard at edit time — the existing red highlight on missing required cells continues to work, but you can still export with required fields blank.
- **Edit history is not retained.** No undo, no "show previous value" beyond the hover tooltip on a green cell.
- **Per-row edited export only.** The frontend always sends the *full* `preview.rows.length` row set when any edit exists. For very large batches this means the request body grows linearly with row count, but Phase 1 batches are small (~16 rows for Richmond), so this is not a concern.

---

## What's next

This wraps up Phase 1B. Future phases (not in scope here) would naturally include:

- Persisted edits (a per-batch `edits.json`, posted on every commit instead of only at export time).
- Per-cell validation rules driven from the same YAML the processors use.
- Multi-vendor support (extend `_PROCESSOR_LOADERS` in `batch_processor.py`).
- Bulk edit / fill-down / find-replace.
- Audit trail of which cells were overridden, by whom, when.
