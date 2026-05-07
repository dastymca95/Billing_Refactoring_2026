# Webapp Phase 1Q Audit Critical Fixes Report

Date: 2026-05-02

## Summary

Phase 1Q implemented the critical fixes from `FULLSTACK_QA_AUDIT_20260502.md`:

- Hardened every backend batch path lookup behind strict generated-format validation.
- Blocked traversal-style batch ids such as `%2E%2E` with `400 {"detail":"Invalid batch id"}`.
- Made delete semantics safe: invalid id returns 400, valid missing batch returns 404, existing valid batch deletes only inside `webapp_data/batches`.
- Fixed the batch picker outside-click boundary so row clicks can switch batches before the dropdown closes.
- Added structured frontend API errors and friendly operator-facing messages.
- Added a backend route contract verifier.
- Updated Docker/local run docs for the moved Docker README, Vite proxy behavior, and stale-backend reset workflow.

No vendor business logic, Richmond/Hopkinsville source files, CLI entrypoints, Dropbox code, AI activation settings, source bills, or `Output/Template.xlsx` were intentionally changed in this phase.

## Audit Findings Addressed

### Safe Batch Paths

Implemented in:

- `webapp/backend/settings.py`
- `webapp/backend/services/batch_store.py`
- `webapp/backend/main.py`

Batch ids now must match:

```text
^batch_\d{8}_\d{6}_\d{3}$
```

`batch_dir(batch_id)` now:

1. Validates the id format.
2. Resolves `BATCHES_ROOT`.
3. Resolves `(BATCHES_ROOT / batch_id)`.
4. Confirms the candidate remains inside `BATCHES_ROOT`.
5. Raises `InvalidBatchIdError("Invalid batch id")` on failure.

FastAPI maps `InvalidBatchIdError` to:

```json
{"detail": "Invalid batch id"}
```

with HTTP 400.

### Endpoints Hardened

All routes/services that call `batch_store.get_batch_dir()`, `get_input_dir()`,
`get_processed_dir()`, `get_export_dir()`, `list_files_in_batch()`, or
`delete_batch()` now inherit strict batch id validation.

This includes:

- `GET /api/batches/{batch_id}`
- `PATCH /api/batches/{batch_id}`
- `DELETE /api/batches/{batch_id}`
- `GET /api/batches/{batch_id}/files`
- `POST /api/batches/{batch_id}/upload`
- file preview/raw/content endpoints
- `POST /api/batches/{batch_id}/process`
- `POST /api/batches/{batch_id}/cancel`
- `GET /api/batches/{batch_id}/preview`
- `GET /api/batches/{batch_id}/manual-review`
- `POST /api/batches/{batch_id}/export`
- `GET /api/batches/{batch_id}/download`
- regions endpoints

Additional hardening:

- `GET /api/batches/{batch_id}/download?filename=...` now strips traversal by requiring the requested filename to be a basename inside the export folder.
- `batch_store.list_batches()` now only lists directories matching the generated batch id pattern.
- `DELETE /api/batches/{batch_id}` now returns 404 for a valid but missing batch.

### Batch Picker

Implemented in:

- `webapp/frontend/src/components/BatchHeader.tsx`

The outside-click ref now wraps the full batch header, including the dropdown,
instead of only the header row. This prevents the document `mousedown` handler
from treating a dropdown row click as outside the picker before `onClick` can
switch the batch.

Friendly fallback labels were also added:

- Active batch with missing name: `Untitled batch`
- Dropdown row with missing `batch_name`: `Untitled batch`
- Missing counts render as `0 file(s)` / `0 inv`

### Frontend Error Normalization

Implemented in:

- `webapp/frontend/src/api.ts`
- `webapp/frontend/src/App.tsx`
- `webapp/frontend/src/components/RenameBatchModal.tsx`
- `webapp/frontend/src/components/DocumentPreviewPanel.tsx`
- `webapp/frontend/src/components/AiFallbackStatusBadge.tsx`
- `webapp/frontend/src/components/pdf_workspace/PdfWorkspace.tsx`
- `webapp/frontend/src/components/pdf_workspace/PdfPageCanvas.tsx`

`api.ts` now exports:

- `ApiError`
- `isApiError(error)`
- `getFriendlyErrorMessage(error, context?)`

`jsonOrThrow()` now parses FastAPI JSON error bodies, preserves `status`,
`statusText`, `detail`, and `rawBody`, and avoids throwing raw strings like:

```text
HTTP 405 Method Not Allowed: {"detail":"Method Not Allowed"}
```

Common user-facing mappings:

- 400 invalid batch id: `Invalid batch. Please refresh and try again.`
- 404: `Batch not found. It may have been deleted.`
- 405: `This action is not available on the running backend. Restart the backend and refresh the app.`
- 422: `Some information is invalid. Please review and try again.`
- network/TypeError: `Could not reach the backend. Make sure the backend is running.`

Raw error objects are now sent to `console.warn(...)` in the updated handlers
instead of being shown to normal operators.

### Route Contract Verification

Added:

- `scripts/verify_backend_routes.py`

Run from the project root:

```powershell
python scripts/verify_backend_routes.py
```

Required routes checked:

- `POST /api/batches`
- `GET /api/batches`
- `GET /api/batches/{batch_id}`
- `PATCH /api/batches/{batch_id}`
- `DELETE /api/batches/{batch_id}`
- `POST /api/batches/{batch_id}/process`
- `POST /api/batches/{batch_id}/cancel`
- `POST /api/batches/{batch_id}/export`
- `GET /api/batches/{batch_id}/regions`
- `PUT /api/batches/{batch_id}/regions`
- `GET /api/ai/status`

### Docs and Runbook Updates

Updated:

- `webapp/README_WEBAPP.md`
- `docs/DOCKER_WEBAPP_README.md`
- `.dockerignore`
- `docker-compose.yml`
- `webapp/frontend/vite.config.ts`

Changes:

- Fixed links from `webapp/README_WEBAPP.md` to `../docs/DOCKER_WEBAPP_README.md`.
- Updated `.dockerignore` allowlist for `docs/DOCKER_WEBAPP_README.md`.
- Documented local backend `http://localhost:8000`.
- Documented local frontend `http://localhost:5173`.
- Clarified that frontend browser fetches use relative `/api`.
- Clarified that `VITE_API_BASE_URL` only changes the Vite dev proxy target, not static build runtime behavior.
- Added stale-backend reset commands: `netstat`, `taskkill`, restart backend/frontend, health check, route verifier, and OpenAPI path inspection.

## Minor Audited Fixes

Implemented in:

- `webapp/backend/services/batch_processor.py`

Fixes:

- Corrected cancelled-run `files_done` calculation from comparing a list against dict keys to summing grouped files whose vendor key produced output.
- For unsupported-only processing, pending vendor-processing timeline stages are now marked skipped with `No supported files in batch` before the run completes.

## Validation Results

### Backend Compile

Command:

```powershell
python -m compileall webapp\backend
```

Result: passed.

### Route Contract

Command:

```powershell
python scripts\verify_backend_routes.py
```

Result: passed.

### Frontend Build

Initial PowerShell command:

```powershell
npm run build
```

Result: blocked by local PowerShell execution policy for `npm.ps1`.

Successful command:

```powershell
npm.cmd run build
```

Result: passed (`tsc -b && vite build`).

### Backend API Smoke with TestClient

Covered:

- create batch
- rename batch
- list batches
- get batch
- `GET /api/batches/%2E%2E` returns 400
- `DELETE /api/batches/%2E%2E` returns 400
- `DELETE /api/batches/batch_20990101_000000_000` returns 404
- cancel idle batch returns `no_active_run`
- valid delete returns success

Result: passed.

### Clean Live Backend Smoke

Started a temporary clean backend on port 8002 and stopped it after the smoke.

Covered:

- health check
- create batch
- rename batch
- get renamed batch
- invalid id GET returns 400
- invalid id DELETE returns 400
- valid missing id DELETE returns 404
- cancel idle returns `no_active_run`
- valid delete returns success
- live OpenAPI contains PATCH batch, cancel, regions, and AI status routes

Result: passed.

### Current `localhost:8000` Observation

`http://localhost:8000/api/health` responded successfully, but live OpenAPI on
port 8000 was still missing:

- `/api/batches/{batch_id}/cancel`
- `/api/batches/{batch_id}/regions`
- `/api/ai/status`

This confirms the stale-backend risk remains in the user's current environment.
I did not terminate the running process during this phase. The clean working-tree
backend was verified on port 8002.

### UI Smoke

Full browser click smoke was not run because this tool session did not have a
browser automation tool or Playwright available without installing additional
packages. The batch-picker fix was verified by code review and the frontend
production build passed. Manual UI check still recommended:

1. Start clean backend and frontend.
2. Create at least two batches.
3. Open the batch dropdown.
4. Click a non-active batch row.
5. Confirm the active batch changes and the dropdown closes after the switch.

### Integrity Checks

Commands:

```powershell
git status --short -- Output\Template.xlsx "Training Bills_Invoices" .env .env.example
git diff --name-only -- Output\Template.xlsx "Training Bills_Invoices"
```

Result: no changes reported for `Output/Template.xlsx`, source training bills,
or `.env` paths.

No AI provider calls were made. No Dropbox export/process flow was triggered.

## Known Limitations

- The live `localhost:8000` process still appears stale and should be restarted
  before operator testing.
- Browser-level UI smoke for the dropdown click behavior remains manual until a
  browser automation tool is available in the session.
- Existing browser-native `confirm(...)` calls still exist for destructive or
  edit-discard actions. They are acceptable temporarily per the audit, but an
  app-native confirm modal remains a future UX cleanup.
- Static frontend builds still require a reverse proxy or a runtime API-base
  implementation if the backend is not served on the same origin under `/api`.

## Recommended Next Phase

1. Stop stale backend/frontend processes and restart from the current working tree.
2. Run the manual UI smoke checklist above.
3. Add a small automated frontend test harness for batch picker click behavior.
4. Convert remaining browser-native confirmations to app-native dialogs.
5. Add a live OpenAPI verification step to the local startup runbook or dev script.
