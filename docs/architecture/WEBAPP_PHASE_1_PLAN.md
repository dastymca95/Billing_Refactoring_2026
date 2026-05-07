# Webapp — Phase 1 Plan

A simple local web UI on top of the existing `Billing_Refactoring_2026` Python/YAML logic. Phase 1 wires Richmond Utilities into a drag-and-drop dashboard so the operator can: upload → process → preview → export.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Frontend  (React + Vite + TypeScript)                              │
│  webapp/frontend/                                                   │
│    DropZone → FileList → DocumentPreview → ResManPreview → Export   │
│         │              │                                            │
│         └──── HTTP (JSON / multipart) ──────┐                       │
│                                              ▼                       │
│  Backend  (FastAPI + uvicorn)                                       │
│  webapp/backend/                                                    │
│    main.py          ← FastAPI app, CORS, mount routers              │
│    api/             ← endpoints (one router per resource)           │
│    services/                                                        │
│      batch_processor.py   ← calls vendor processors                 │
│      vendor_detection.py  ← simple heuristic from filename / hdr    │
│      document_preview.py  ← CSV/XLSX → JSON; PDF → raw bytes        │
│      resman_preview.py    ← reads Output/Template.xlsx + filled rows│
│         │                                                           │
│         └──── existing project Python ──────┐                       │
│                                              ▼                       │
│  Existing logic (REUSED, NEVER duplicated)                          │
│    Training Bills_Invoices/.../process_richmond_utilities.py        │
│    utils/dropbox_uploader.py                                        │
│    utils/service_period_resolver.py                                 │
│    config/vendors/richmond_utilities.yaml                           │
│    Output/Template.xlsx, Properties/, Gl Codes/, Vendors/           │
└─────────────────────────────────────────────────────────────────────┘
```

## Folder structure

```
webapp/
├── backend/
│   ├── __init__.py
│   ├── main.py                     ← FastAPI app + CORS + routers
│   ├── settings.py                 ← project root + paths constants
│   ├── api/
│   │   ├── __init__.py
│   │   ├── batches.py              ← create batch, list files, status
│   │   ├── uploads.py               ← upload files to a batch
│   │   ├── processing.py            ← run processor + return summary
│   │   ├── preview.py               ← document preview + ResMan preview
│   │   └── export.py                ← write final xlsx + download
│   └── services/
│       ├── __init__.py
│       ├── batch_store.py           ← create / locate batch dirs
│       ├── vendor_detection.py      ← heuristic vendor router
│       ├── document_preview.py      ← CSV/XLSX → JSON; PDF/img → bytes
│       └── resman_preview.py        ← serialize generated rows for UI
├── frontend/
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── api.ts                   ← typed fetch wrappers
│       ├── types.ts
│       ├── styles.css
│       └── components/
│           ├── DropZone.tsx
│           ├── FileList.tsx
│           ├── DocumentPreviewPanel.tsx
│           ├── BatchActionsPanel.tsx
│           ├── ResManTemplatePreview.tsx
│           ├── ManualReviewPanel.tsx
│           └── ExportPanel.tsx
└── README_WEBAPP.md

webapp_data/                         ← runtime, gitignored
└── batches/
    └── batch_YYYYMMDD_HHMMSS_<id>/
        ├── input/                   ← uploaded files
        ├── processed/               ← processor output
        ├── export/                  ← final ResMan xlsx
        ├── logs/
        └── manual_review/
```

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/batches` | Create batch → returns `{ batch_id }` |
| POST | `/api/batches/{id}/upload` | Upload one file (multipart) |
| GET  | `/api/batches/{id}/files` | List uploaded files + per-file status |
| POST | `/api/batches/{id}/detect` | Heuristic vendor detection per file |
| POST | `/api/batches/{id}/process` | Run vendor processor; returns summary |
| GET  | `/api/batches/{id}/preview` | Generated ResMan rows as JSON |
| GET  | `/api/batches/{id}/manual-review` | Manual-review issues as JSON |
| POST | `/api/batches/{id}/export` | Write final xlsx; returns path |
| GET  | `/api/batches/{id}/download` | Stream the final xlsx |
| GET  | `/api/batches/{id}/files/{filename}/raw` | Stream raw file (PDF/image) |
| GET  | `/api/batches/{id}/files/{filename}/preview` | Parsed CSV/XLSX → JSON |
| DELETE | `/api/batches/{id}` | Delete batch folder |
| GET  | `/api/health` | Liveness probe |

## Frontend layout

```
┌───────────────────────────────────────────────────────────────┐
│  Top bar:  Billing Refactoring 2026  •  [Clear]  [New Batch]  │
├──────────────┬────────────────────────────────────────────────┤
│  Left rail   │  Main panel                                    │
│              │                                                │
│  DropZone    │  DocumentPreviewPanel                          │
│  ───────     │  (selected file: PDF / image / csv table)      │
│              │                                                │
│  FileList    │                                                │
│   • a.csv    │  ──────────────────────────────────────────    │
│   • b.csv    │  ResManTemplatePreview (collapsible)            │
│   • c.pdf    │   table with all generated rows                │
│              │                                                │
│  Actions:    │                                                │
│  [Process]   │  ──────────────────────────────────────────    │
│  [Preview]   │  ManualReviewPanel  (collapsible)               │
│  [Export]    │                                                │
└──────────────┴────────────────────────────────────────────────┘
```

## Vendor detection (Phase 1, very light)

`services/vendor_detection.py` returns one of:

- `richmond_utilities` if the filename matches a Richmond pattern (`*BillingHistory*Recent*` or starts with two digit-blocks separated by `_`)
- `unknown` otherwise (frontend offers a manual-pick dropdown — currently only Richmond is wired)

Future phases will add more rules without changing the API contract.

## How Richmond Utilities integration works

1. The CLI script `process_richmond_utilities.py` is **refactored** to expose a callable function:

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

   `main()` becomes a thin CLI wrapper that calls this function with the original defaults. **The existing CLI behavior is unchanged.**

2. The webapp backend imports this function and calls it with batch-specific paths. The same YAML, the same Dropbox helper, the same service-period resolver.

3. Result `ProcessBatchResult` is a dataclass with:
   - `success: bool`
   - `summary: dict` (counts)
   - `resman_workbook_path / manual_review_workbook_path / debug_csv_path / log_path: Path`
   - `invoices: list[dict]` — JSON-ready rows for the preview
   - `manual_review_rows: list[dict]` — JSON-ready issues
   - `errors: list[str]`

## In scope (Phase 1)

- ✅ Backend skeleton with all endpoints listed above
- ✅ Frontend skeleton with all components listed above
- ✅ Drag-and-drop upload
- ✅ File list with per-file status
- ✅ Document preview for CSV / XLSX (table) and PDF (embed)
- ✅ Process button → calls Richmond processor
- ✅ ResMan preview table
- ✅ Manual review panel
- ✅ Export → download Excel
- ✅ Refactor `process_richmond_utilities.py` without breaking CLI

## Out of scope (Phase 1)

- ❌ Authentication
- ❌ Database
- ❌ Cloud deployment
- ❌ Vendor detection ML/AI
- ❌ Per-vendor processors beyond Richmond Utilities
- ❌ Real-time progress (processing is synchronous)
- ❌ OCR for scanned PDFs
- ❌ Word document support
- ❌ Multi-user support
- ❌ Editing rows in-browser (read-only preview)

## Risks

- Refactoring `process_richmond_utilities.py` is the riskiest change. Mitigation: keep the entire existing function bodies; only wrap the existing `main()` body in a callable that takes the four path parameters (defaulting to current globals). Smoke-test the CLI before declaring done.
- Synchronous processing on the backend can stall the frontend for large batches. Phase 1 is fine for ≤20 files; later phases can add background tasks.
- Frontend has to handle binary preview (PDF/image) which requires correct content-type from backend. Phase 1 will use direct stream endpoints with `application/pdf` / `image/*` content-type.
