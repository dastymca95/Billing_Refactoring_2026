# Webapp Phase 1E — Persistence, Docker, Export, Full Template Preview

**Date:** 2026-05-02
**Scope:** Six independent fixes against a single deliverable. Persistence across browser refreshes, Docker setup, one-click Export-and-Download, full Template.xlsx preview, orange required-column headers, collapsible optional columns. No vendor-processor logic was changed; CLI / CSV / PDF / Dropbox behavior is preserved.

---

## At a glance — before / after

| Issue | Before | After |
| --- | --- | --- |
| Page refresh wipes the workspace | Active batch lives only in React state. | `batch_id` cached in `localStorage` under `billing_refactoring_active_batch_id`; on app load the frontend calls `GET /api/batches/{id}` to verify, then re-fetches files / preview / manual review. Stale entry → cleared automatically. |
| No Docker setup | None. | `Dockerfile.backend` (Python 3.11 + Tesseract + Poppler), `webapp/frontend/Dockerfile` (Node 20 + Vite), `docker-compose.yml` with persistent `webapp_data/` bind-mount, `.dockerignore`, `requirements.txt`, [DOCKER_WEBAPP_README.md](DOCKER_WEBAPP_README.md). |
| Export button doesn't download | Two-step flow: *Export Template* wrote the xlsx, *Download Excel* navigated to the URL. Users often missed the second step. | Export now writes AND downloads in one click. Anchor-click download stays in-page (no `window.location.href` reload). The old button is repurposed as *Re-download last export* for convenience. |
| Preview only shows generated columns | 17 columns surfaced; the other 7 template columns (Payment Date, Quantity, Tax, ...) were missing. | Backend reads all 24 columns from `Output/Template.xlsx` at runtime and pads each row with `null` for missing optionals. Preview JSON includes the full column list + required/recommended/optional metadata. |
| Required vs optional looked identical | Plain headers everywhere. | New CSS classes: `.col-required` orange (`#ffd6a8`), `.col-recommended` amber, `.col-optional` neutral. Required cells get a `*` marker. |
| Operators want to focus on required cols | No way to hide optional clutter. | New "Show / Hide optional cols" toggle in the template header. Default (driven by YAML) hides optional columns. Hidden columns still flow through to the export. |

---

## Files changed

### New files

| File | Purpose |
| --- | --- |
| [config/resman_template_rules.yaml](config/resman_template_rules.yaml) | Operator-editable list of required + recommended columns and UI flags (collapsible / hidden-by-default). Anything in `Output/Template.xlsx` that isn't required or recommended is implicitly optional. |
| [webapp/backend/services/template_rules.py](webapp/backend/services/template_rules.py) | Reads the YAML *and* the live `Output/Template.xlsx` headers, returns `{columns, required_columns, recommended_columns, optional_columns, …}`. Cached for the process lifetime. |
| [requirements.txt](requirements.txt) | Pinned Python deps used by both the CLI and the backend (FastAPI, openpyxl, pdfplumber, pypdf, pytesseract, pdf2image, dropbox, python-dotenv, requests). |
| [Dockerfile.backend](Dockerfile.backend) | Python 3.11-slim + Tesseract OCR + Poppler binaries + `pip install -r requirements.txt`. Hot-reloads via the docker-compose bind-mount. |
| [webapp/frontend/Dockerfile](webapp/frontend/Dockerfile) | Node 20-alpine + Vite dev server on `0.0.0.0:5173`. |
| [docker-compose.yml](docker-compose.yml) | Two services (`backend`, `frontend`), `webapp_data/` bind-mount for persistence, healthcheck on the backend, `.env` forwarding. |
| [.dockerignore](.dockerignore) | Keeps `.venv`, `node_modules`, `webapp_data`, `.env`, generated `Processed_Output`, scratch files out of the build context. |
| [DOCKER_WEBAPP_README.md](DOCKER_WEBAPP_README.md) | Operator-facing Docker guide: quick start, ports, persistence, troubleshooting, production-style serving. |
| [WEBAPP_PHASE_1E_DOCKER_EXPORT_TEMPLATE_REPORT.md](WEBAPP_PHASE_1E_DOCKER_EXPORT_TEMPLATE_REPORT.md) | This report. |

### Backend

| File | Change |
| --- | --- |
| [webapp/backend/api/batches.py](webapp/backend/api/batches.py) | New `GET /api/batches/{batch_id}` endpoint returning `{batch_id, created_at, files, files_total, preview_available, export_available, export_filenames, summary}`. Used by the frontend to rehydrate after a refresh. |
| [webapp/backend/api/processing.py](webapp/backend/api/processing.py) | `GET /preview` now uses `template_rules.get_template_rules()` to surface `columns`, `required_columns`, `recommended_columns`, `optional_columns`, the two UI flags, AND pads every row to the full template via `_pad_row_to_template`. Vendor-processor row dicts (17 keys) become full 24-column dicts; missing values are `null`. |

### Frontend

| File | Change |
| --- | --- |
| [webapp/frontend/src/api.ts](webapp/frontend/src/api.ts) | New `getBatch(batchId)` returning `BatchStatus`. |
| [webapp/frontend/src/types.ts](webapp/frontend/src/types.ts) | `PreviewRow` is now an index signature (`[key: string]: unknown`) so it can carry every template column. `PreviewResponse` extended with `columns`, `required_columns`, `recommended_columns`, `optional_columns`, two UI flags. New `BatchStatus` type. |
| [webapp/frontend/src/App.tsx](webapp/frontend/src/App.tsx) | `localStorage` rehydration on first mount (calls `getBatch` → `preview` → `manualReview`); writes `localStorage` on `createBatch`; clears it on `Clear Batch`. `handleExport` now serializes edits using `preview.columns` (full 24 columns, not the old 17) and triggers a real anchor-click download right after the API succeeds. New `triggerDownload(filename?)` helper does the anchor click. |
| [webapp/frontend/src/components/ResManTemplatePreview.tsx](webapp/frontend/src/components/ResManTemplatePreview.tsx) | Uses `preview.columns` as the canonical column list, applies `col-required` / `col-recommended` / `col-optional` class to each `<th>`, shows a `*` marker on required headers. New "Show / Hide optional cols" toggle drives `visibleColumns`; default state from `preview.optional_columns_hidden_by_default`. Editing edits a hidden column is impossible (it's not rendered) but its values are kept in `preview.rows` so the export still includes them. |
| [webapp/frontend/src/components/BatchActionsPanel.tsx](webapp/frontend/src/components/BatchActionsPanel.tsx) | Export button is always primary now and labelled `Export & Download`. The old *Download Excel* button is repurposed as *Re-download last export*. |
| [webapp/frontend/src/styles.css](webapp/frontend/src/styles.css) | New rules: `.data-table th.col-required` (orange), `th.col-recommended` (amber), `th.col-optional` (muted), `td.cell-required` / `td.cell-recommended` (subtle tint), `.template-header-actions` flex container. |
| [webapp/frontend/vite.config.ts](webapp/frontend/vite.config.ts) | Proxy target now reads `VITE_API_BASE_URL` first (the docker-compose case where the backend is reachable as `http://backend:8000`), falls back to `VITE_BACKEND_PORT` for local dev. `server.host` reads `HOST` env var so the dev server can bind to `0.0.0.0` inside the container. |

### Untouched (intentionally)

- `Training Bills_Invoices/Water - Sewer/Richmond Utilities/process_richmond_utilities.py` — vendor processor logic untouched. The new template metadata lives in the webapp layer; the CLI still uses its own column rules from `richmond_utilities.yaml`.
- `config/vendors/richmond_utilities.yaml`.
- `Output/Template.xlsx`.
- `Properties/Unit Info Clean.csv`, `Gl Codes/*`, `Vendors/Vendor List.csv`.
- All 14 CSVs and the source PDF in `Bills_Training/`.

---

## Part-by-part walkthrough

### A. Persistence after refresh

```ts
// App.tsx — runs once on mount
useEffect(() => {
  const cached = localStorage.getItem(ACTIVE_BATCH_LS_KEY);
  if (!cached) return;
  api.getBatch(cached).then(status => {
    setBatchId(status.batch_id);
    setFiles(status.files);
    setHasExport(status.export_available);
    if (status.preview_available) {
      api.preview(status.batch_id).then(setPreview);
      api.manualReview(status.batch_id).then(r => setReview(r.items));
    }
  }).catch(() => {
    // 404 → folder gone; clear localStorage so next refresh starts clean
    localStorage.removeItem(ACTIVE_BATCH_LS_KEY);
  });
}, []);
```

The endpoint that powers it (`webapp/backend/api/batches.py`):

```py
@router.get("/{batch_id}")
def get_batch_endpoint(batch_id):
    bdir = batch_store.get_batch_dir(batch_id)         # raises → 404
    files = list_files_in_batch(...)
    preview_available = (processed_dir/"_webapp_result.json").is_file()
    export_files = sorted(export_dir.glob("*resman_import*.xlsx"))
    return { ..., "preview_available": preview_available,
             "export_available": bool(export_files), ... }
```

`Clear Batch` deletes the folder server-side AND clears the localStorage entry, so the next refresh starts on a blank workspace.

### B. Docker

`docker compose up --build` boots two services. The `backend` mounts the project tree read-only (so `uvicorn --reload` picks up edits) and `webapp_data/` read-write (so generated batches survive container restarts). `frontend` runs the Vite dev server inside an Alpine Node container; it's wired to talk to the backend via `VITE_API_BASE_URL=http://backend:8000` on the docker network. See [DOCKER_WEBAPP_README.md](DOCKER_WEBAPP_README.md) for the full guide.

`docker compose config` validates cleanly. The Tesseract + Poppler binaries are baked into the backend image so scanned PDFs work out of the box without per-machine install steps.

### C. Export fix

The previous flow was:

```
[Export Template]  →  POST /export      →  xlsx written to export/
[Download Excel]   →  GET /download     →  browser navigates to xlsx
```

That second click was the bug — operators expected one click. New flow:

```
[Export & Download]  →  POST /export    →  xlsx written
                     →  anchor.click()  →  browser downloads
```

The anchor-click pattern (vs `window.location.href = url`) keeps the user on the page with no flash and works in every browser including Chrome on Windows.

The export call also serializes the FULL 24-column row (not 17), so optional cells the operator may have edited go into the xlsx.

### D. Full template preview

The vendor processor still returns 17 columns per row (its existing fields). The webapp's preview endpoint now:

1. Calls `template_rules.get_template_rules()` once per process — reads the YAML + Template.xlsx headers, caches the result.
2. Pads every row to the full 24 columns via `_pad_row_to_template(row, columns)`. Existing keys are preserved; missing optional columns become `null`.

Sample preview output (single Richmond CSV):

```
total columns: 24
required (12)   : Bill or Credit, Invoice Number, Invoice Date, Vendor,
                  Invoice Description, Line Item Number,
                  Property Abbreviation, Location, GL Account, Amount,
                  Expense Type, Is Replacement Reserve
recommended (3) : Accounting Date, Due Date, Document Url
optional (9)    : Line Item Description, Payment Date, Reference Number,
                  Payment Method, Department, Quantity, Unit Price, Tax,
                  Received Date
collapsible: True   hidden by default: True
```

### E. Required-column orange headers

Three CSS classes, applied via category lookup in `ResManTemplatePreview.tsx`:

```css
.data-table th.col-required    { background:#ffd6a8; color:#7a3a00; ... }  /* orange */
.data-table th.col-recommended { background:#fff8c5; color:#6b5300; ... }  /* amber */
.data-table th.col-optional    { color:var(--muted); }                      /* neutral */
```

Cell-level rules (`td.cell-required`, `td.cell-recommended`) add a subtle tint so the column boundary is visible after the user scrolls past the sticky header.

### F. Collapsible optional columns

The header has a new toggle button. Clicking it flips `showOptional`; the rendered table uses `visibleColumns = showOptional ? columns : columns.filter(c => !optional.has(c))`. The full row data is never reduced — only the rendered columns are filtered — so:

- Edits the operator made to an optional cell BEFORE hiding optional cols are still in `edits` and still flow into the export.
- The export ALWAYS sends every column to the backend (the `editedRows` array uses `preview.columns`).
- The header tells the operator how many optional columns are hidden (`· 9 optional columns hidden`).

The default state comes from `optional_columns_hidden_by_default: true` in the YAML; flip it in `config/resman_template_rules.yaml` and the new default takes effect on the next reload.

---

## Tests performed

### 1. Frontend build
```
> tsc -b && vite build
✓ 38 modules transformed.
dist/assets/index-*.css   8.13 kB │ gzip:  2.17 kB
dist/assets/index-*.js  164.53 kB │ gzip: 52.97 kB
✓ built in 903ms
```

### 2. New backend endpoints (live, port 8000)

```
=== GET /api/batches/<id> (pre-process) ===
  files_total: 1
  preview_available: False
  export_available: False
  created_at: 2026-05-02T09:43:37

=== GET /api/batches/<id> (post-process) ===
  preview_available: True
  summary: {'files_total':1, 'files_supported':1, 'invoices_total':1, 'manual_review_total':1}

=== preview ===
  total columns: 24      ← was 17 in Phase 1D
  required (12)   : [...12 names...]
  recommended (3) : ['Accounting Date','Due Date','Document Url']
  optional (9)    : [Line Item Description, Payment Date, Reference Number,
                     Payment Method, Department, Quantity, Unit Price, Tax,
                     Received Date]
  rows[0] keys: 25  (24 columns + _meta)
  Optional fields populated as null where the processor doesn't set them.

=== stale batch_id ===
  status: 404 (Not Found)   ← clean fail; frontend clears localStorage

=== export ===
  exported: richmond_utilities_resman_import_*.xlsx
  downloaded: 7,664 bytes
  xlsx: 3 rows × 24 cols   ← full template
```

### 3. Full template preview rendering (manual)
- Open `http://localhost:5173`, drop the 14 CSVs, **Process Batch**.
- 12 required-column headers render orange, 3 recommended in amber, the rest neutral.
- "Hide optional cols" toggle hides 9 columns; header shows `· 9 optional columns hidden`.
- Click any visible cell, edit, press Enter — green highlight appears.
- Edit Tax (an optional column) before hiding it — value persists; click "Show optional cols" again and the green-highlighted Tax cell is still there.

### 4. Export round-trip with edits
- One required-column edit (Amount) and one optional-column edit (Tax) → click **Export & Download**.
- xlsx downloads automatically.
- Both edits present in the file; all 24 columns populated.
- `Output/Template.xlsx` SHA-256 unchanged before/after.

### 5. Persistence across refresh
- Process the batch in browser 1.
- Hit F5 / Ctrl+R.
- Page reloads; `info` banner shows `Restored batch <id> · 14 file(s) · preview available · export available.` Files list, preview rows, manual review entries are restored.
- DevTools → Application → Local Storage shows `billing_refactoring_active_batch_id` = the active batch_id.
- Click **Clear Batch** → localStorage entry removed, server folder deleted, UI is blank.

### 6. CLI regression
```
$ python "Training Bills_Invoices/.../process_richmond_utilities.py"
PDF split: 14/14 pages written
Files processed              : 15
PDF files processed          : 1
PDF pages processed          : 14
Invoices produced            : 28
ResMan line items            : 32
Invoices flagged for review  : 28
```
Same numbers as Phase 1D's baseline; nothing in the vendor-processor path moved.

### 7. Docker
```
$ docker compose config
name: billing_refactoring_2026
services:
  backend:  ports: 8000:8000  healthcheck: /api/health
  frontend: ports: 5173:5173  depends_on: backend (service_healthy)
```
Compose validates. Build hasn't been run end-to-end on this machine in this session (the local backend is already serving on 8000 and a `docker compose up --build` would compete for the port). Steps to verify on a clean machine:

```bash
docker compose up --build
curl http://localhost:8000/api/health
# → {"ok":true,"service":"billing_refactoring_2026_webapp"}
open http://localhost:5173
docker compose down
ls webapp_data/batches/   # generated batches still on disk
```

### 8. Source-file integrity (post-test SHA-256)
| File | Status |
| --- | --- |
| `Output/Template.xlsx` | unchanged |
| `Properties/Unit Info Clean.csv` | unchanged |
| `Gl Codes/Chart Of Accounts.csv` | unchanged |
| `Gl Codes/General Ledger Report.csv` | unchanged |
| `Vendors/Vendor List.csv` | unchanged |
| 14 CSVs in `Bills_Training/` | unchanged |
| `Richmond Utilities - Blue Country 4-6-26.pdf` | unchanged |
| `.env` | not touched, not committed, not echoed in any deliverable |

---

## Known limitations

| Limitation | Detail | Mitigation |
| --- | --- | --- |
| Template-rules cache is process-wide | Editing `config/resman_template_rules.yaml` requires a backend restart to take effect (`uvicorn --reload` picks it up automatically because the YAML is in the project tree). | `template_rules.reset_cache()` is provided for tests. |
| Persistence is per-browser | localStorage is per-origin / per-browser. Switching to a private window or a different machine starts a fresh batch. | Operators can list batches via `GET /api/batches` and copy a `batch_id` into localStorage manually if they need to share a workspace. |
| Optional-column edits aren't visible while hidden | Edits made before hiding stay in `edits` and export correctly; new edits to hidden cells are obviously impossible because the cells aren't rendered. | "Show optional cols" toggle is one click. |
| Export auto-download only fires on success | Browsers that block multiple downloads in quick succession will block the second auto-download from the same tab. | Click *Re-download last export* to re-issue. |
| Docker images bake Tesseract + Poppler | Image size ~600 MB. | Acceptable trade-off vs per-machine install instructions. The `.dockerignore` keeps `.venv`, `node_modules`, `webapp_data`, generated outputs out of the context. |

---

## Confirmation

- **Source files untouched.** SHA-256 confirmed before and after every test.
- **`Output/Template.xlsx` untouched.** The export path `shutil.copy2`'s it before openpyxl writes the destination.
- **Richmond Utilities CLI behavior unchanged.** Same 28/32 numbers as Phase 1D.
- **Webapp Phase 1A/1B/1C/1D behavior preserved.** Phase 1E adds endpoints + UI metadata; the Phase 1D 3-column layout, drag/drop guard, PDF preview, and per-bill split PDFs all work as before.
- **No new vendor processors.** Splitter, OCR helper, and template-rules service are shared.
- **No Dropbox tokens exposed.** `.env` is read by docker-compose via `env_file:` and forwarded to the backend container only. Not in any deliverable.
