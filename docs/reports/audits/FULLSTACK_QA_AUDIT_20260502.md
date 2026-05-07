# Full-Stack QA Audit - 2026-05-02

Project: Billing Refactoring 2026 Web Console  
Workspace: `C:\Users\Dasty\PycharmProjects\Billing_Refactoring_2026`  
Audit type: source review plus live backend/frontend smoke tests

## 1. Executive summary

The reported rename failure is reproduced and root-caused. The frontend at `http://localhost:5173` is calling a backend on `localhost:8000` that is not serving the current backend source. The current source defines and registers `PATCH /api/batches/{batch_id}`, but the live `8000` OpenAPI schema does not include it. Direct PATCH against `8000` returns exactly:

```json
{"detail":"Method Not Allowed"}
```

The same stale backend also returns the old batch-list shape, only `batch_id` and `path`, while the current frontend expects `batch_name`, `files_count`, `invoices_count`, and `export_available`. That reproduces the dropdown symptom: rows render as `files · inv` because the fields are missing.

A clean backend started from the current working tree on `8001` exposes the expected routes, and a clean frontend on `5174` pointed at `8001` creates and renames batches correctly. That means the 405 is an environment/stale-process problem, not a missing route in current source.

There are also current-source issues that should be fixed before the next implementation phase:

- Critical: `batch_id` is not validated or contained. `GET /api/batches/%2E%2E` is accepted as batch id `..` and resolves outside `webapp_data/batches`. The DELETE path uses the same resolver and could be dangerous. Do not test DELETE traversal.
- High: the clean-stack batch picker renders valid rows but row clicks do not switch batches. The document-level `mousedown` close handler unmounts the picker before the row `onClick` executes.
- High: frontend error handling still turns backend JSON into operator-facing raw strings in several paths.
- High: live `8000` lacks current routes: `PATCH /api/batches/{id}`, regions, AI status, and cancel.
- Medium: `VITE_API_BASE_URL` is only used by the Vite dev proxy, not by `src/api.ts`; a static build still fetches relative `/api`.

No implementation fixes were made in this audit. I created this report only.

## 2. Environment and run configuration findings

Expected ports:

| Surface | Expected port | Evidence |
| --- | ---: | --- |
| Backend FastAPI | 8000 | `webapp/backend/main.py`, `docker-compose.yml`, `webapp/README_WEBAPP.md` |
| Frontend Vite | 5173 | `webapp/frontend/vite.config.ts`, `docker-compose.yml`, `webapp/README_WEBAPP.md` |
| Audit clean backend | 8001 | Started during audit to avoid disturbing existing `8000` |
| Audit clean frontend | 5174 | Started during audit to point at clean backend |

Frontend API base behavior:

- `webapp/frontend/src/api.ts` always calls relative URLs such as `/api/batches`.
- `webapp/frontend/vite.config.ts` proxies `/api` to `proxyTarget`.
- `proxyTarget` is `VITE_API_BASE_URL` if present, else `http://localhost:${VITE_BACKEND_PORT || 8000}`.
- `VITE_API_BASE_URL` is not read by the browser runtime. It only affects the Vite dev server proxy.
- In Docker compose, `VITE_API_BASE_URL=http://backend:8000` is correct for the frontend container's Vite proxy.
- In a static built frontend, `VITE_API_BASE_URL` will not make client fetches go to another host unless `api.ts` is changed to use it.

Current live port usage found:

```text
127.0.0.1:8000 LISTENING python PID 43804
127.0.0.1:8000 LISTENING python PID 28356
[::1]:5173     LISTENING node   PID 55544
```

The two Python PIDs both show uvicorn commands:

```powershell
python -m uvicorn webapp.backend.main:app --port 8000 --reload --log-level warning
python -m uvicorn webapp.backend.main:app --reload --port 8000
```

This can be normal for uvicorn reload parent/child, but the served route set is stale. The source file `webapp/backend/api/batches.py` has `PATCH`, while live `8000` does not.

Docker status:

- `docker compose ps` returned no running compose services.
- Docker is not currently the thing serving `8000` or `5173`.
- Docker compose would fail to bind `8000`/`5173` while the local processes are already running.
- Compose bind-mounts the current source tree into `/app`, so `docker compose up --build` should serve current code after ports are freed.

Documentation mismatch:

- The requested root file `DOCKER_WEBAPP_README.md` is deleted/moved in the working tree.
- A Docker readme exists at `docs/DOCKER_WEBAPP_README.md`.
- `webapp/README_WEBAPP.md` still links to `../DOCKER_WEBAPP_README.md`, which is currently broken from `webapp/`.
- `.dockerignore` allowlists `!DOCKER_WEBAPP_README.md`, not `!docs/DOCKER_WEBAPP_README.md`.

## 3. Commands to verify environment

Recommended PowerShell checks:

```powershell
netstat -ano | findstr :8000
netstat -ano | findstr :5173
Get-CimInstance Win32_Process -Filter "ProcessId=<PID>" | Select-Object ProcessId,Name,CommandLine
Invoke-RestMethod http://localhost:8000/api/health
$o = Invoke-RestMethod http://localhost:8000/openapi.json
$o.paths.PSObject.Properties | ForEach-Object {
  $path=$_.Name
  $_.Value.PSObject.Properties.Name | ForEach-Object { "$_ $path" }
} | Sort-Object
```

Clean local run:

```powershell
# Stop stale local Python/Node processes first, after confirming PIDs.
taskkill /PID <backend_pid> /F
taskkill /PID <frontend_pid> /F

# Backend
python -m uvicorn webapp.backend.main:app --reload --port 8000

# Frontend
cd webapp/frontend
npm.cmd run dev

# Health and route check
Invoke-RestMethod http://localhost:8000/api/health
Invoke-RestMethod http://localhost:8000/openapi.json
```

Docker reset/run:

```powershell
docker compose down
netstat -ano | findstr :8000
netstat -ano | findstr :5173
docker compose up --build
docker compose ps
docker compose logs -f backend
docker compose logs -f frontend
Invoke-RestMethod http://localhost:8000/api/health
```

## 4. Backend route inventory

Current source route map from `webapp.backend.main:app`:

| Method | Path | Source | Request body | Used by frontend |
| --- | --- | --- | --- | --- |
| GET | `/api/health` | `main.py` | none | `api.health` |
| POST | `/api/batches` | `api/batches.py` | optional `CreateBatchBody` | `api.createBatch` |
| GET | `/api/batches` | `api/batches.py` | none | `api.listBatches` |
| GET | `/api/batches/{batch_id}` | `api/batches.py` | none | `api.getBatch` |
| PATCH | `/api/batches/{batch_id}` | `api/batches.py` | `UpdateBatchBody` | `api.updateBatch`, `api.renameBatch` |
| DELETE | `/api/batches/{batch_id}` | `api/batches.py` | none | `api.deleteBatch` |
| GET | `/api/batches/{batch_id}/files` | `api/batches.py` | none | `api.listFiles` |
| GET | `/api/batches/{batch_id}/progress` | `api/batches.py` | none | `api.getBatchProgress` |
| POST | `/api/batches/{batch_id}/upload` | `api/uploads.py` | multipart `file` | `api.uploadFile` |
| GET | `/api/batches/{batch_id}/files/{filename}/preview` | `api/preview.py` | none | `api.filePreview` |
| GET | `/api/batches/{batch_id}/files/{filename}/raw` | `api/preview.py` | none | `api.fileRawUrl` helper, currently not observed in UI |
| GET | `/api/batches/{batch_id}/files/{filename}/content` | `api/preview.py` | none | `api.fileContentUrl` |
| POST | `/api/batches/{batch_id}/detect` | `api/processing.py` | none | not used by current UI |
| POST | `/api/batches/{batch_id}/process` | `api/processing.py` | query `sync` optional | `api.process` |
| POST | `/api/batches/{batch_id}/cancel` | `api/processing.py` | none | `api.cancelBatch` |
| GET | `/api/batches/{batch_id}/preview` | `api/processing.py` | none | `api.preview` |
| GET | `/api/batches/{batch_id}/manual-review` | `api/processing.py` | none | `api.manualReview` |
| POST | `/api/batches/{batch_id}/export` | `api/export.py` | optional `ExportRequest` | `api.exportBatch` |
| GET | `/api/batches/{batch_id}/download` | `api/export.py` | optional query `filename` | `api.downloadUrl` |
| GET | `/api/batches/{batch_id}/regions` | `api/regions.py` | none | `api.listRegions` |
| PUT | `/api/batches/{batch_id}/regions` | `api/regions.py` | `ReplaceRegionsBody` | `api.replaceRegions` |
| POST | `/api/batches/{batch_id}/regions` | `api/regions.py` | `Region` | `api.addRegion`, currently not observed in UI |
| DELETE | `/api/batches/{batch_id}/regions/{region_id}` | `api/regions.py` | none | `api.deleteRegion`, currently not observed in UI |
| GET | `/api/ai/status` | `api/ai_status.py` | none | `api.getAiStatus` |
| GET | `/` | `main.py` | none | operator/dev only |

Live `8000` route map is stale. It only exposed:

```text
GET /api/health
POST /api/batches
GET /api/batches
GET /api/batches/{batch_id}
DELETE /api/batches/{batch_id}
GET /api/batches/{batch_id}/files
GET /api/batches/{batch_id}/progress
POST /api/batches/{batch_id}/upload
GET /api/batches/{batch_id}/files/{filename}/preview
GET /api/batches/{batch_id}/files/{filename}/raw
GET /api/batches/{batch_id}/files/{filename}/content
POST /api/batches/{batch_id}/detect
POST /api/batches/{batch_id}/process
GET /api/batches/{batch_id}/preview
GET /api/batches/{batch_id}/manual-review
POST /api/batches/{batch_id}/export
GET /api/batches/{batch_id}/download
```

Missing live `8000` routes:

- `PATCH /api/batches/{batch_id}`
- `POST /api/batches/{batch_id}/cancel`
- all `/regions` endpoints
- `GET /api/ai/status`

## 5. Frontend API inventory

All fetches are in `webapp/frontend/src/api.ts`; components use the wrapper except URL helpers.

| Function | Method | URL | Request body | Main component usage | Backend match in current source |
| --- | --- | --- | --- | --- | --- |
| `health` | GET | `/api/health` | none | `App` startup | yes |
| `createBatch` | POST | `/api/batches` | `batch_name`, `document_mode`, AI fields | `App.ensureBatch`, new batch modal | yes |
| `updateBatch` | PATCH | `/api/batches/{id}` | metadata fields | rename modal submit | yes, stale live no |
| `renameBatch` | PATCH | `/api/batches/{id}` | `{batch_name}` | not observed in current component usage | yes, duplicate wrapper |
| `listBatches` | GET | `/api/batches` | none | `BatchHeader` list | yes, stale live old shape |
| `getBatch` | GET | `/api/batches/{id}` | none | restore/switch | yes |
| `getBatchProgress` | GET | `/api/batches/{id}/progress` | none | process polling | yes |
| `uploadFile` | POST | `/api/batches/{id}/upload` | multipart file | dropzone upload | yes |
| `listFiles` | GET | `/api/batches/{id}/files` | none | refresh after upload | yes |
| `filePreview` | GET | `/api/batches/{id}/files/{filename}/preview` | none | document panel | yes |
| `fileRawUrl` | GET helper | `/api/batches/{id}/files/{filename}/raw` | none | not observed | yes |
| `fileContentUrl` | GET helper | `/api/batches/{id}/files/{filename}/content` | none | PDF/image preview | yes |
| `process` | POST | `/api/batches/{id}/process` or `?sync=1` | none | Process button | yes |
| `cancelBatch` | POST | `/api/batches/{id}/cancel` | none | Stop button | yes, stale live no |
| `preview` | GET | `/api/batches/{id}/preview` | none | process/switch/restore | yes |
| `manualReview` | GET | `/api/batches/{id}/manual-review` | none | process/switch/restore | yes |
| `exportBatch` | POST | `/api/batches/{id}/export` | optional `{edited_rows}` | template export | yes |
| `downloadUrl` | GET helper | `/api/batches/{id}/download` | none | download anchor | yes |
| `deleteBatch` | DELETE | `/api/batches/{id}` | none | delete batch | yes |
| `getAiStatus` | GET | `/api/ai/status` | none | AI badge | yes, stale live no |
| `listRegions` | GET | `/api/batches/{id}/regions` | none | PDF workspace | yes, stale live no |
| `replaceRegions` | PUT | `/api/batches/{id}/regions` | `{regions}` | PDF workspace save | yes, stale live no |
| `addRegion` | POST | `/api/batches/{id}/regions` | `RegionHint` | not observed | yes |
| `deleteRegion` | DELETE | `/api/batches/{id}/regions/{region_id}` | none | not observed | yes |

Mismatches and risks:

- Current source is internally aligned for major routes.
- Live `8000` is not aligned with current frontend.
- `api.ts` has a raw `jsonOrThrow` that throws `HTTP <status> <statusText>: <raw response text>`.
- `RenameBatchModal` strips only the `HTTP ...:` prefix, so JSON such as `{"detail":"Method Not Allowed"}` remains visible.
- `BatchHeader` assumes `batch_name`, `files_count`, and `invoices_count` exist. It should defensively fallback even if the backend is stale or corrupted.

## 6. Rename 405 reproduction and root cause

Live stale stack:

1. Opened `http://localhost:5173`.
2. Created a batch through the app-native new batch modal.
3. UI displayed current batch as `batch_20260502_164832_316`, not the typed name.
4. Toast displayed `Created batch "undefined" · mode=auto_detect.`
5. Opened rename modal.
6. Submitted `UI QA Renamed Should Fail`.
7. Modal inline error displayed raw `{"detail":"Method Not Allowed"}`.
8. Browser console also showed stale AI route failure: `HTTP 404 Not Found` for `/api/ai/status`.

Direct backend reproduction against `8000`:

```powershell
$created = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/batches `
  -ContentType "application/json" `
  -Body '{"batch_name":"QA Rename Direct","document_mode":"auto_detect"}'

Invoke-RestMethod -Method Patch -Uri "http://localhost:8000/api/batches/$($created.batch_id)" `
  -ContentType "application/json" `
  -Body '{"batch_name":"QA Renamed Direct"}'
```

Result:

```text
PATCH_FAILED
405
{"detail":"Method Not Allowed"}
```

Direct backend positive control against clean `8001`:

- `POST /api/batches` returned `batch_id`, `batch_name`, and `metadata`.
- `PATCH /api/batches/{id}` returned updated metadata.
- `GET /api/batches/{id}` persisted the new name.

Root cause:

- The current frontend is hitting a stale backend on `localhost:8000`.
- The stale backend is missing the PATCH route and has the old batch-list response shape.
- The UI error is raw because frontend error normalization is too thin.

Plausible explanations:

- PyCharm or an old terminal started uvicorn before the newer backend code was loaded.
- Multiple uvicorn reload processes are present and the live child is stale.
- A different working directory or interpreter path is serving an older `webapp.backend` module.
- Browser cache is not the primary cause here because direct backend calls reproduce the 405.
- Docker is not currently the active cause because no compose containers are running.

## 7. Batch lifecycle QA

### Create batch

Stale `8000`:

- Direct `POST /api/batches` returned only `{"batch_id": ...}`.
- UI lost the supplied name and displayed the raw batch id.
- Toast showed `Created batch "undefined"`.

Clean `8001`/`5174`:

- Created `UI QA Clean Stack`.
- UI showed the custom name.
- Clean batch list row showed `UI QA Clean Renamed 0 files · 0 inv`.
- Metadata persisted in `batch_metadata.json`.

Result: current source works if paired with current backend. Stale backend breaks the flow.

### Rename batch

Stale `8000`:

- UI PATCH failed with 405.
- Modal showed raw JSON detail.

Clean `8001`/`5174`:

- UI rename succeeded.
- Toast showed `Renamed batch to "UI QA Clean Renamed".`
- Direct GET confirmed persisted metadata.

Result: source route and frontend method are correct; deployed/local process is stale.

### Delete batch

I did not delete real or audit-created batches. Deleting local data needs explicit action-time confirmation. Source review found a real issue:

- `DELETE /api/batches/batch_does_not_exist` returns `{"deleted": true}` instead of 404.
- `batch_id` is not validated before constructing paths.
- Because `GET /api/batches/%2E%2E` resolves as batch id `..`, DELETE must be considered unsafe until batch id validation is added.

### Switch batch

Clean `8001`/`5174`:

- Batch picker renders valid rows.
- Clicking a row such as `batch_20260502_162207_363 9 files · 9 inv · ✓` closes the dropdown but does not switch the active batch.

Likely source cause:

- In `BatchHeader.tsx`, `wrapRef` is attached only to `.batch-header-row`.
- The picker list is rendered as a sibling outside `wrapRef`.
- The document-level `mousedown` handler sees picker row clicks as outside clicks and closes the picker before the button `onClick` runs.

Result: current-source switch batch flow is blocked.

### Old batches with missing metadata

Clean current backend:

- `GET /api/batches` falls back to `batch_id` for missing `batch_name`.
- Existing legacy batches show raw batch ids as primary labels.

Recommendation:

- Backend fallback should use a readable generated label such as `Untitled batch - 2026-05-02 16:22`.
- Frontend should fallback defensively to `batch_id` or `Untitled batch`, and numeric counts should fallback to `0`.

## 8. Upload/process/cancel/export QA

To avoid triggering possible Dropbox uploads for real bills, I used a harmless local dummy `.txt` file and clean backend `8001` for endpoint mechanics. Existing processed batches were inspected read-only for richer preview data.

Dummy batch tested:

- Created `QA Dummy Unsupported`.
- Uploaded `billing_qa_dummy.txt`.
- `GET /files` returned one `unknown` unsupported file.
- `POST /process?sync=1` completed with `files_total=1`, `files_supported=0`, `files_unsupported=1`.
- `GET /progress` returned `status=completed`, `percent=100`, and declared stages.
- `GET /preview` returned the full 24-column template shape with `rows=[]`.
- `GET /manual-review` returned `items=[]`.
- `POST /export` without edits returned `exported=[]`.
- `POST /export` with one edited row wrote `resman_import_edited_*.xlsx`.
- `GET /download` downloaded the edited export.
- OpenPyXL verified exported values:
  - `Invoice Number='QA-001'`
  - `Vendor='QA Vendor'`
  - `Amount=12.34`
- `POST /cancel` with no active run returned `status=no_active_run`.

Issues found:

- `HEAD /download` returns 405. The UI uses GET, so this is not user-visible, but health/download probes should use GET.
- Unsupported-file processing marks many non-applicable stages as `pending` even after completion. This is not fatal but makes the timeline semantically odd.
- In cancellation summary code, `files_done=sum(len(v) for v in grouped.values() if v in by_vendor)` compares a list to dict keys, so cancelled progress may report wrong `files_done`.

Existing processed batch read-only check:

- `batch_20260502_162207_363` had 9 files, 9 invoices, 56 rows, and 3 manual-review items.
- Preview returned full columns, required columns, recommended columns, optional columns, and row counts as expected.
- I did not re-process real Richmond/Hopkinsville bills because the processors can upload support documents to Dropbox.

## 9. Document preview and region QA

Direct API tests on clean backend:

- `GET /api/batches/{id}/files/billing_qa_dummy.txt/content` returned:
  - `200 OK`
  - `Content-Disposition: inline`
  - `Content-Type: text/plain; charset=utf-8`
  - `X-Content-Type-Options: nosniff`
- `GET /api/batches/{id}/regions` returned empty list when no regions exist.
- `GET /api/batches/batch_does_not_exist/regions` returned `200` with empty regions by design.
- Invalid label POST returned `400` with JSON `detail`.
- Valid region POST persisted a region.
- DELETE region removed it.

Risks:

- UI region save uses `setSaveError(\`Save failed: ${e}\`)`, so raw HTTP/JSON can still surface.
- Region endpoints inherit the unvalidated `batch_id` path traversal risk.
- Full PDF page navigation/zoom/marking was not verified visually because the batch-switch UI bug prevented selecting an existing PDF batch in the clean UI without modifying app state.

## 10. Template grid QA

Source review:

- `TemplateWorkspace` supports Required, Issues, and All views.
- Required view keeps required plus recommended columns.
- Issues view keeps required, recommended, `Document Url`, `Reference Number`, and `Invoice Description`.
- All view uses every backend column.
- `ResManTemplatePreview` single-click selects row and double-click opens exact cell editor.
- Enter commits; Escape cancels.
- Edited cells get green background and outline.
- Required missing cells get red tint.
- Required headers get `col-required` and marker.
- Document Url renders as compact `Open` chip.
- Export maps merged edited rows across all full preview columns, not only visible columns.

Observed:

- Build passed, so the TS contracts compile.
- Export with edited rows writes expected values.

Not fully verified in UI:

- Double-click edit on an existing processed row, because batch switching is currently blocked.

Risks:

- The table is a plain HTML table with all rows rendered. For hundreds or thousands of rows, rendering and editing could become sluggish.
- `ResManTemplatePreview` still has an internal optional-column toggle, even though `TemplateWorkspace` already curates views. It appears harmless but is conceptually duplicated.

## 11. Error handling findings

Raw/error-prone paths:

| Location | Current behavior | Classification |
| --- | --- | --- |
| `api.ts` `jsonOrThrow` | throws `HTTP status text: raw body` | should become structured `ApiError` |
| `RenameBatchModal` | strips prefix but leaves JSON detail | modal inline error, needs JSON parsing |
| `App.handleFiles` | `setError(String(e))` | should become toast or friendly banner |
| `App.handleProcess` | `setError(String(e))`, failed progress raw | should become toast and console detail |
| `App.handleCancel` | `setError(String(e))` | toast |
| `App.handleRefreshPreview` | `setError(String(e))` | toast or inline preview error |
| `App.handleExport` | `setError(String(e))` | toast |
| `App startup health` | `Backend is not reachable: ${String(e)}` | acceptable banner, but clean up text |
| `App restore preview/batch` | includes raw `String(e)` | should be friendly text plus console detail |
| `AiFallbackStatusBadge` | internal state uses raw `String(e)` but visible label is friendly | acceptable if raw stays out of UI |
| `DocumentPreviewPanel` | user sees `Could not load preview.` | acceptable |
| `PdfWorkspace` region save | `Save failed: ${e}` | should be friendly inline error |
| `BatchActionsBar` | browser `confirm` for delete and stop | acceptable temporarily, app-native confirm preferred |

Specific reproduced user-facing raw string:

```text
{"detail":"Method Not Allowed"}
```

Recommendation:

- Parse JSON response bodies in `jsonOrThrow`.
- Convert `detail` to a plain message.
- Map 405 on rename to: `This backend does not support renaming yet. Restart the backend and refresh the app.`
- Keep raw payloads in `console.warn`.

## 12. Performance and rendering findings

Positive source findings:

- Progress polling is 500 ms and stops on `completed`, `failed`, or `cancelled`.
- Polling interval is cleaned up on unmount.
- PDF rendering caches documents per file URL with max cache size 4.
- PDF render task is cancelled on file/page/zoom changes.
- PDF loading overlay is delayed 250 ms to reduce flicker.
- Parent callback is held in a ref to avoid render restarts on unrelated parent rerenders.
- Table filtering and summaries use `useMemo`.

Risks:

- `waitForProcessingDone` loops while interval polling is also active, doubling progress requests during processing.
- PDF cache eviction only removes the promise from `_docCache`; it does not call `doc.destroy()`, so memory may linger after multiple large PDFs.
- Template grid renders all visible rows and columns without virtualization.
- Batch list endpoint recomputes live counts and vendor detection for each batch on every list call. With many batches and PDFs, this can become slow.
- `GET /api/batches/{id}` detects vendor for every file on rehydrate/switch. For large batches, switching will be slow.
- Unsupported completed runs leave most timeline stages as pending, which can make the UI look unfinished.

## 13. Docker/local mismatch risks

Docker compose is generally safe for dev:

- Backend bind-mounts current project source read-only to `/app`.
- `webapp_data` is bind-mounted read/write.
- Frontend bind-mounts current frontend source.
- Frontend proxy points to `http://backend:8000` inside Docker.

Risks:

- Host ports conflict with local uvicorn/node. Currently `8000` and `5173` are occupied.
- A direct `docker run` of an old image, outside compose, could serve old baked code.
- The frontend dev server proxy target is fixed when Vite starts. Changing `VITE_BACKEND_PORT` or `VITE_API_BASE_URL` requires restarting Vite.
- Static frontend builds still call relative `/api`; docs imply `VITE_API_BASE_URL` can configure built static bundles, but source does not implement that.

Safe Docker verification sequence:

```powershell
docker compose down
netstat -ano | findstr :8000
netstat -ano | findstr :5173
docker compose up --build
Invoke-RestMethod http://localhost:8000/api/health
$o = Invoke-RestMethod http://localhost:8000/openapi.json
$o.paths.PSObject.Properties.Name
```

## 14. Security and sensitive-file findings

Ignored local/sensitive paths:

- `.env` is ignored.
- `webapp_data/` is ignored.
- `Training Bills_Invoices/` is ignored.
- `Gl Codes/` and `GL Codes/` are ignored.
- `Properties/` is ignored.
- `Vendors/` is ignored.
- `Output/` is ignored.
- `Old Scripts/` is ignored.
- `.venv/`, frontend `node_modules/`, and frontend `dist/` are ignored.

Tracked file check:

- `git ls-files` showed zero tracked files under the sensitive data directories and `.env`.
- `.env` exists locally but is ignored. I did not print its contents.
- Secret-pattern scan of tracked files found placeholder/config references such as API key variable names and Dropbox token environment names. I did not identify committed secret values in tracked files from the redacted category scan.

Critical path issue:

- `settings.batch_dir(batch_id)` returns `BATCHES_ROOT / batch_id` without validation or containment.
- `batch_store.get_batch_dir`, `get_input_dir`, `get_processed_dir`, `get_export_dir`, and `delete_batch` trust that path.
- Direct read test: `GET /api/batches/%2E%2E` returned a valid-looking response for batch id `..`.
- This must be fixed before trusting DELETE, upload, regions, preview, or export endpoints.

Required fix:

- Enforce `^batch_\d{8}_\d{6}_\d{3}$` for existing generated ids, or a strict allowlist helper.
- Resolve the path and assert it is under `BATCHES_ROOT`.
- Reject invalid ids with 400.
- Make DELETE return 404 if the batch does not exist.

## 15. Prioritized fixes

### Critical

1. Stop stale backend/frontend processes and restart from current source.
2. Add strict `batch_id` validation and path containment before any batch filesystem access.
3. Make `DELETE /api/batches/{id}` return 404 for missing/invalid ids and never operate outside `BATCHES_ROOT`.

### High

1. Fix `BatchHeader` picker outside-click logic so clicking rows switches batches.
2. Add route-contract smoke tests that fail if live OpenAPI lacks current frontend routes.
3. Normalize frontend API errors and remove raw `{"detail":...}` from operator UI.
4. Add defensive frontend fallbacks for stale/malformed batch list rows.
5. Restart Vite whenever proxy environment changes; document this in the runbook.

### Medium

1. Implement a true client API base URL or correct docs to say Vite proxy only.
2. Fix cancellation `files_done` calculation.
3. Improve completed timeline semantics for unsupported-only batches.
4. Add app-native confirmation modal for destructive delete.
5. Reconcile/move Docker readme links.

### Low

1. Remove unused/duplicate `api.renameBatch` or standardize call sites on it.
2. Destroy evicted pdf.js documents.
3. Consider table virtualization for large processed batches.
4. Add friendly generated labels for legacy batches missing metadata.

## 16. Recommended implementation plan

1. Environment reset/runbook:
   - Identify and stop current `8000` and `5173` processes.
   - Restart backend and frontend from this repository.
   - Verify `/openapi.json` includes PATCH, cancel, regions, and AI status.

2. Backend hardening:
   - Add `validate_batch_id` and `safe_batch_dir`.
   - Use it in every batch path helper.
   - Return 400 for invalid ids and 404 for missing valid ids.
   - Add tests for `%2E%2E`, missing batch delete, and normal batch CRUD.

3. Frontend contract/error layer:
   - Replace `jsonOrThrow` with structured `ApiError`.
   - Parse FastAPI `{detail}` payloads.
   - Add friendly messages for 404, 405, 415, 422.
   - Keep raw details in console only.

4. Batch UI:
   - Fix picker click-outside containment.
   - Add defensive fallbacks in `BatchHeader`.
   - Re-test create, rename, switch, delete confirmation, and stale localStorage.

5. Workflow QA:
   - With explicit approval for Dropbox/upload side effects, run a real Richmond and Hopkinsville batch.
   - Verify process/cancel mid-run, preview, edit, export, download, and Document Url.

6. Docs and Docker:
   - Update README Docker links.
   - Clarify dev proxy vs static build API base behavior.
   - Add route verification commands to the README.

## 17. Commands used

Representative commands:

```powershell
git status --short
netstat -ano | findstr :8000
netstat -ano | findstr :5173
Get-CimInstance Win32_Process -Filter "ProcessId=43804 OR ProcessId=28356 OR ProcessId=55544"
Invoke-RestMethod http://localhost:8000/api/health
Invoke-RestMethod http://localhost:8000/openapi.json
Invoke-RestMethod -Method Post http://localhost:8000/api/batches -ContentType "application/json" -Body '{"batch_name":"QA Rename Direct","document_mode":"auto_detect"}'
Invoke-RestMethod -Method Patch http://localhost:8000/api/batches/<id> -ContentType "application/json" -Body '{"batch_name":"QA Renamed Direct"}'
python -m uvicorn webapp.backend.main:app --host 127.0.0.1 --port 8001 --log-level info
npm.cmd run dev -- --host 127.0.0.1 --port 5174
npm.cmd run build
python -m compileall webapp\backend
docker compose ps
docker compose config --no-interpolate
git check-ignore -v .env webapp_data "Training Bills_Invoices" Vendors "Gl Codes" "Old Scripts" Output
git ls-files -- "Training Bills_Invoices" "Gl Codes" Vendors Properties Output "Old Scripts" .env
```

Browser automation was used against:

- `http://localhost:5173` for stale-stack reproduction.
- `http://localhost:5174` for clean-stack positive create/rename and batch-picker testing.

## 18. Files inspected

Primary files:

- `webapp/backend/main.py`
- `webapp/backend/settings.py`
- `webapp/backend/api/batches.py`
- `webapp/backend/api/uploads.py`
- `webapp/backend/api/preview.py`
- `webapp/backend/api/processing.py`
- `webapp/backend/api/export.py`
- `webapp/backend/api/regions.py`
- `webapp/backend/api/ai_status.py`
- `webapp/backend/services/batch_store.py`
- `webapp/backend/services/batch_processor.py`
- `webapp/backend/services/cancel_registry.py`
- `webapp/backend/services/document_preview.py`
- `webapp/backend/services/template_rules.py`
- `webapp/backend/services/vendor_detection.py`
- `webapp/backend/services/ai_fallback.py`
- `webapp/frontend/src/api.ts`
- `webapp/frontend/src/App.tsx`
- `webapp/frontend/src/types.ts`
- `webapp/frontend/src/components/BatchHeader.tsx`
- `webapp/frontend/src/components/BatchActionsBar.tsx`
- `webapp/frontend/src/components/RenameBatchModal.tsx`
- `webapp/frontend/src/components/TemplateWorkspace.tsx`
- `webapp/frontend/src/components/ResManTemplatePreview.tsx`
- `webapp/frontend/src/components/DocumentPreviewPanel.tsx`
- `webapp/frontend/src/components/AiFallbackStatusBadge.tsx`
- `webapp/frontend/src/components/pdf_workspace/PdfWorkspace.tsx`
- `webapp/frontend/src/components/pdf_workspace/PdfPageCanvas.tsx`
- `webapp/frontend/src/hooks/useResizablePanel.ts`
- `webapp/frontend/vite.config.ts`
- `webapp/frontend/package.json`
- `webapp/frontend/Dockerfile`
- `docker-compose.yml`
- `Dockerfile.backend`
- `.gitignore`
- `.dockerignore`
- `webapp/README_WEBAPP.md`
- `docs/DOCKER_WEBAPP_README.md`

## 19. Known limitations

- I did not run a real Richmond/Hopkinsville processing job because current processors can upload support documents to Dropbox. That would be an external transfer of bill documents.
- I did not delete any real or audit-created batch folders because deletion needs explicit action-time confirmation.
- I did not visually test PDF region drawing after switching to a processed batch because the clean-stack batch-switch UI is currently blocked.
- I did not inspect `.env` contents or print secrets.
- I did not make source-code fixes in this phase.
- Audit-created batches remain in `webapp_data` with names beginning `QA` or `UI QA`.

