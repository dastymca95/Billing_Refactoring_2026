# Webapp — Phase 1 Implementation Report

**Generated:** 2026-05-01
**Scope:** Local web UI on top of the existing Python/YAML billing logic. Drag-and-drop → process → preview → export. Wired to **Richmond Utilities** only.

---

## What was built

### 1. Refactor of `process_richmond_utilities.py`

The legacy `main()` was repackaged into a callable function:

```python
def process_richmond_utilities_batch(
    input_folder: Path | None = None,
    output_folder: Path | None = None,
    template_path: Path | None = None,
    config_path: Path | None = None,
    run_context: dict | None = None,
) -> ProcessBatchResult:
    ...
```

- All four path parameters default to the original module-level paths so the **CLI is unchanged**.
- A new `ProcessBatchResult` dataclass is returned with: `success`, `return_code`, `summary`, `invoices` (JSON-serializable preview rows), `manual_review_rows`, `resman_workbook_path`, `manual_review_workbook_path`, `debug_csv_path`, `log_path`, `errors`.
- Two helper serializers (`_invoice_to_preview_dict`, `_manual_review_to_dict`) translate internal dataclasses into JSON-friendly dicts so the FastAPI layer can return them as-is.
- `main()` is now a 3-line CLI wrapper around `process_richmond_utilities_batch()`.

CLI regression test (no arguments): produces 14 invoices, 16 line items, 9 flagged for review — **identical to the pre-refactor run**.

### 2. FastAPI backend

```
webapp/backend/
├── __init__.py
├── main.py                   FastAPI app + CORS + router mounting
├── settings.py               Project root + path constants + batch ID generator
├── api/
│   ├── batches.py            POST/GET /api/batches, GET /files, DELETE
│   ├── uploads.py            POST /api/batches/{id}/upload
│   ├── preview.py            GET /files/{name}/preview, /raw
│   ├── processing.py         POST /process, GET /preview, GET /manual-review
│   └── export.py             POST /export, GET /download
└── services/
    ├── batch_store.py        Per-batch folder lifecycle
    ├── vendor_detection.py   Heuristic vendor router
    ├── document_preview.py   CSV/XLSX → JSON
    └── batch_processor.py    Calls process_richmond_utilities_batch + caches result
```

Phase 1 supports only Richmond Utilities. The vendor detector is a small `_DETECTORS` list — adding more vendors later is one function each.

### 3. React + Vite + TypeScript frontend

```
webapp/frontend/
├── package.json              react 18 + vite 5 + typescript 5
├── tsconfig.json
├── vite.config.ts            proxies /api → http://localhost:8000
├── index.html
└── src/
    ├── main.tsx
    ├── App.tsx               top-level state machine
    ├── api.ts                typed fetch wrappers
    ├── types.ts
    ├── styles.css            simple, professional dashboard look
    └── components/
        ├── DropZone.tsx
        ├── FileList.tsx
        ├── DocumentPreviewPanel.tsx     PDF (<embed>) / image (<img>) / CSV/XLSX (table) / metadata
        ├── BatchActionsPanel.tsx        Process / Preview / Export / Download
        ├── ResManTemplatePreview.tsx    Collapsible table with row highlighting
        └── ManualReviewPanel.tsx        Collapsible review list with reason tooltips
```

`npm run build` succeeds with no TypeScript errors. 38 modules transformed; bundle size 156 KB / 50 KB gzipped.

### 4. Documentation

- `WEBAPP_PHASE_1_PLAN.md` — architecture, scope, endpoints, components.
- `webapp/README_WEBAPP.md` — operator-facing run instructions.
- This report.

## Files created

| Path | Lines |
| --- | ---: |
| `WEBAPP_PHASE_1_PLAN.md` | 188 |
| `WEBAPP_PHASE_1_IMPLEMENTATION_REPORT.md` (this file) | — |
| `webapp/README_WEBAPP.md` | 167 |
| `webapp/backend/__init__.py` | 1 |
| `webapp/backend/main.py` | 56 |
| `webapp/backend/settings.py` | 39 |
| `webapp/backend/api/__init__.py` | 0 |
| `webapp/backend/api/batches.py` | 49 |
| `webapp/backend/api/uploads.py` | 47 |
| `webapp/backend/api/preview.py` | 43 |
| `webapp/backend/api/processing.py` | 76 |
| `webapp/backend/api/export.py` | 41 |
| `webapp/backend/services/__init__.py` | 0 |
| `webapp/backend/services/batch_store.py` | 57 |
| `webapp/backend/services/vendor_detection.py` | 73 |
| `webapp/backend/services/document_preview.py` | 88 |
| `webapp/backend/services/batch_processor.py` | 159 |
| `webapp/frontend/package.json` | 23 |
| `webapp/frontend/tsconfig.json` | 22 |
| `webapp/frontend/vite.config.ts` | 19 |
| `webapp/frontend/index.html` | 13 |
| `webapp/frontend/.gitignore` | 4 |
| `webapp/frontend/src/main.tsx` | 11 |
| `webapp/frontend/src/App.tsx` | 184 |
| `webapp/frontend/src/api.ts` | 86 |
| `webapp/frontend/src/types.ts` | 105 |
| `webapp/frontend/src/styles.css` | 252 |
| `webapp/frontend/src/components/DropZone.tsx` | 56 |
| `webapp/frontend/src/components/FileList.tsx` | 56 |
| `webapp/frontend/src/components/DocumentPreviewPanel.tsx` | 105 |
| `webapp/frontend/src/components/BatchActionsPanel.tsx` | 41 |
| `webapp/frontend/src/components/ResManTemplatePreview.tsx` | 116 |
| `webapp/frontend/src/components/ManualReviewPanel.tsx` | 88 |

## Files modified

| Path | Change |
| --- | --- |
| `Training Bills_Invoices/Water - Sewer/Richmond Utilities/process_richmond_utilities.py` | Wrapped `main()` body in `process_richmond_utilities_batch(...)`; added `ProcessBatchResult` dataclass + serializer helpers; original CLI behavior unchanged |
| `.gitignore` | Added `webapp_data/`, `webapp/frontend/node_modules/`, `webapp/frontend/dist/` |

## Files NOT modified

- `Output/Template.xlsx`
- `Gl Codes/*.csv`
- `Properties/*.csv`
- `Vendors/Vendor List.csv`
- `Bills_Training/*.csv` (any vendor)
- `config/vendors/*.yaml`
- `utils/*.py` (Dropbox uploader and service-period resolver are imported as-is)
- `Old Scripts/` (read-only inspection only, in earlier waves)

## How to run

### Backend

```powershell
".\.venv\Scripts\python.exe" -m uvicorn webapp.backend.main:app --reload --port 8000
```

Health check: http://localhost:8000/api/health · API docs: http://localhost:8000/docs

### Frontend (one-time install + dev server)

```bash
cd webapp/frontend
npm install
npm run dev
```

Open http://localhost:5173.

## End-to-end smoke test (results)

Ran the full pipeline against the 14 Richmond Utilities CSVs:

```
POST /api/batches                                → batch_id
14 × POST /api/batches/{id}/upload               → 14 files uploaded
GET  /api/batches/{id}/files                     → 14 files; vendor=richmond_utilities (95% confidence)
GET  /api/batches/{id}/files/{name}/preview      → table; 4 headers, 143 rows
POST /api/batches/{id}/process                   → summary {files_total: 14, files_supported: 14, invoices_total: 14, manual_review_total: 9}
GET  /api/batches/{id}/preview                   → invoice_count=14, row_count=16
GET  /api/batches/{id}/manual-review             → 9 items
POST /api/batches/{id}/export                    → 1 file exported
GET  /api/batches/{id}/download                  → HTTP 200, 9355 bytes
DELETE /api/batches/{id}                         → cleanup OK
```

Sample row from the preview JSON:

```json
{
  "Invoice Number": "341340.0094 Apr 26",
  "Invoice Date": "04/30/2026",
  "Vendor": "Richmond Utilities",
  "Invoice Description": "04/01/26-04/30/26 - 254-20 254 Lombardy St",
  "Property Abbreviation": "BCA",
  "Location": "254-20",
  "GL Account": "6930",
  "Line Item Description": "04/01/26-04/30/26 - 254-20 254 Lombardy St - Gas service",
  "Amount": 31.42,
  "Expense Type": "General",
  "Is Replacement Reserve": false,
  "Due Date": "05/15/2026",
  "Document Url": "https://www.dropbox.com/scl/fi/.../34134000_94_BillingHistory_Recent (1).csv?... &dl=1"
}
```

Numbers match the CLI output exactly (14 invoices, 16 rows, 9 flagged, 14 successful Dropbox uploads).

## Known limitations

- **Synchronous processing.** The `/process` endpoint blocks until the run completes. ~5 seconds for 14 Richmond files; would need a background queue for very large batches.
- **One vendor wired.** Detection returns `unknown` for non-Richmond files; they're listed but not processed.
- **No PDF text extraction.** Phase 1 just embeds the PDF for visual review — it doesn't OCR or pull explicit dates from the document. The service-period resolver still falls through to reading-rows or batch override.
- **No editing in browser.** Preview is read-only. To correct flagged rows the operator edits the YAML or the source file and re-runs.
- **No persistence beyond disk.** Batches are folders; if you delete `webapp_data/`, you lose the cache. Re-uploading + re-processing reproduces the same output.

## Next steps (out of scope for Phase 1)

1. Wire the next vendor processor (Alabama Power, EPB Fiber, etc.) by adding a detector + `_PROCESSOR_LOADERS` entry. The frontend automatically gets the new vendor — no UI changes required.
2. Add background processing (FastAPI `BackgroundTasks` or RQ) so larger batches don't block the request thread.
3. Add an in-browser dropdown to override `vendor_key` for files detected as `unknown`.
4. Add PDF text extraction so explicit service/reading dates can populate `service_period_rules.document_explicit_dates`.
5. Editable manual-review panel: let the operator pick a Location for "building-only" matches without editing YAML.
6. Production packaging: serve the built frontend from FastAPI static files; one process to launch.

## Confirmation: nothing in the existing project broke

- ✅ Richmond Utilities CLI: identical 14/16/9 numbers post-refactor.
- ✅ All YAML / CSV / template / Bills_Training source files preserve their original LastWriteTime.
- ✅ `Old Scripts/` untouched.
- ✅ Frontend builds (`npm run build` succeeds; no TS errors).
- ✅ Backend imports cleanly (`/api/health` returns OK on first hit).
- ✅ End-to-end pipeline smoke test passes (every endpoint returns expected data, final XLSX downloads).
