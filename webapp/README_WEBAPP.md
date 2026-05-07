# Billing Refactoring 2026 — Webapp (Phase 1)

A simple local web UI on top of the existing Python/YAML billing logic. Drag bills in, click **Process Batch**, review the generated ResMan template, optionally edit cells inline, click **Export Template**, download the Excel.

> Phase 1 supports **Richmond Utilities only**. Other vendors are detected as `unknown` and listed for manual handling.
>
> Phase 1B (this build) adds **inline cell editing** to the ResMan preview — click any cell to edit, press Enter to save, Escape to cancel. Edits are merged into the exported workbook.
>
> Phase 1C adds **official Richmond Utilities PDF bill** support (in addition to the existing CSV/XLSX billing-history files). Drop a Richmond Utilities PDF into the upload area; the backend OCRs each page and produces one ResMan invoice per page. Bills with extraction issues are flagged in the manual-review panel and can be corrected via inline editing before exporting.
>
> Phase 1D rewires the workspace into a **3-column layout** (compact sidebar · collapsible document preview · ResMan template + bottom drawer for manual review), fixes the **PDF preview** (inline iframe via a new `/content` endpoint), fixes the **drag/drop bug** that caused Chrome to navigate to a dropped PDF, and adds **per-bill PDF splitting** so each ResMan row from a multi-bill PDF gets its own Dropbox link.
>
> Phase 1E adds **batch persistence across browser refreshes** (active `batch_id` cached in `localStorage`, restored via `GET /api/batches/{id}`), wires up a **one-click Export & Download** button, makes the preview show **every column from `Output/Template.xlsx`** (with required-column orange headers and collapsible optional columns driven by `config/resman_template_rules.yaml`), and ships a **Docker setup** (`Dockerfile.backend`, `webapp/frontend/Dockerfile`, `docker-compose.yml`, `.dockerignore`, `requirements.txt`) so the whole app boots with `docker compose up --build`. See [DOCKER_WEBAPP_README.md](../docs/DOCKER_WEBAPP_README.md) and [WEBAPP_PHASE_1E_DOCKER_EXPORT_TEMPLATE_REPORT.md](../WEBAPP_PHASE_1E_DOCKER_EXPORT_TEMPLATE_REPORT.md).
>
> Phase 1F onboards a **second supported vendor — Hopkinsville Water Environment Authority** — and adds a **real-time progress bar** in the sidebar. The progress bar polls `GET /api/batches/{id}/progress` every 750 ms while a batch is processing and shows the current step ("OCR page 7 of 14", "Uploading to Dropbox…", "Building ResMan preview", etc.) plus counts (files done, pages done, invoices created, rows created, warnings). See [HOPKINSVILLE_WATER_IMPLEMENTATION_REPORT.md](../HOPKINSVILLE_WATER_IMPLEMENTATION_REPORT.md).
>
> Phase 1G ships eight QA fixes. **Background-task processing** so the progress bar updates smoothly throughout a run (the previous request blocked, freezing the bar at 5%). **Multi-batch management** with named batches, rename, switch, delete-with-confirm — see the new dropdown in the topbar. **Strict Location + Property Abbreviation validation** — `Location` is cleared when it doesn't match a unit in `Properties/Unit Info Clean.csv`, `Property Abbreviation` is mandatory; both flagged on failure. **Exact bill-total reconciliation** — sum-of-line-items must equal the bill total; sub-cent diffs auto-apply to the largest line, larger gaps flagged. **Training data audit** — 66 City of Henderson misfiles in the Hopkinsville folder were moved to a new vendor folder; 253 root duplicates archived. See [HOPKINSVILLE_WATER_QA_FIX_REPORT.md](../HOPKINSVILLE_WATER_QA_FIX_REPORT.md).

---

## What it does

1. Drag-and-drop CSV / XLSX / PDF / image files into the browser.
2. Backend auto-detects the vendor (Richmond Utilities for now) and stores files in a per-batch folder.
3. Click **Process Batch** → backend calls the existing `process_richmond_utilities_batch(...)` Python function, which uses the same YAML rules, Dropbox uploader, and service-period resolver as the CLI.
4. Inspect the generated rows in a table, see manual-review flags, and download the final Excel.

The webapp **never duplicates business rules**. All logic stays in `config/vendors/*.yaml` + the existing Python helpers under `utils/` and the vendor processors.

## Architecture

```
webapp/
├── backend/                # FastAPI
│   ├── main.py             # app + CORS + /api/health
│   ├── settings.py         # paths
│   ├── api/                # one router per resource
│   │   ├── batches.py
│   │   ├── uploads.py
│   │   ├── preview.py      # /preview, /raw, /content (inline)
│   │   ├── processing.py
│   │   └── export.py
│   └── services/           # batch_store, vendor_detection, document_preview, batch_processor
└── frontend/               # React + Vite + TypeScript
    ├── package.json
    ├── vite.config.ts      # proxies /api → http://localhost:8000
    └── src/
        ├── App.tsx                    # 3-column workspace + global drag/drop guard
        ├── api.ts
        ├── types.ts
        └── components/
            ├── DropZone.tsx           # depth-counter + preventDefault
            ├── FileList.tsx
            ├── DocumentPreviewPanel.tsx   # iframe via /content, collapsible
            ├── BatchActionsPanel.tsx
            ├── ResManTemplatePreview.tsx  # Phase 1B inline edits
            └── ManualReviewPanel.tsx      # bottom drawer, collapsible

webapp_data/                # runtime, gitignored
└── batches/<batch_id>/
    ├── input/<filename>
    ├── processed/<vendor>/...
    └── export/<filename>.xlsx
```

## How to run

### One-time setup

Backend dependencies are already in the project's `.venv` (`fastapi`, `uvicorn`, `python-multipart`, plus the existing `pyyaml`, `openpyxl`, `dropbox`, `python-dotenv`). If they aren't:

```powershell
".\.venv\Scripts\pip.exe" install fastapi "uvicorn[standard]" python-multipart
```

Frontend dependencies — install once:

```bash
cd webapp/frontend
npm install
```

### Run (two terminals)

Terminal 1 — **backend**, from project root:

```powershell
".\.venv\Scripts\python.exe" -m uvicorn webapp.backend.main:app --reload --port 8000
```

```bash
"./.venv/Scripts/python.exe" -m uvicorn webapp.backend.main:app --reload --port 8000
```

Terminal 2 — **frontend**:

```bash
cd webapp/frontend
npm run dev
```

Open http://localhost:5173 in your browser. Vite proxies `/api/*` to the backend on port 8000.

### API base URL behavior

- Local backend: http://localhost:8000
- Local frontend: http://localhost:5173
- Frontend fetch calls use relative `/api/...` URLs.
- In Vite dev mode, `webapp/frontend/vite.config.ts` proxies `/api` to the backend. `VITE_API_BASE_URL` changes only that dev proxy target; it is not a browser runtime setting for a static build.
- `VITE_BACKEND_PORT=8001 npm run dev` points the dev proxy at a different local backend port.
- If you change `VITE_API_BASE_URL` or `VITE_BACKEND_PORT`, restart the Vite dev server.

### Stale backend reset runbook

Use this if the browser shows route errors such as `Method Not Allowed`, or if `/openapi.json` does not list current routes like `PATCH /api/batches/{batch_id}`.

```powershell
# Find backend/frontend listeners
netstat -ano | findstr :8000
netstat -ano | findstr :5173

# Stop a stale process by PID
taskkill /F /PID <PID>

# Restart backend from the current working tree
".\.venv\Scripts\python.exe" -m uvicorn webapp.backend.main:app --reload --port 8000

# Restart frontend from webapp/frontend
npm run dev

# Back in the project root, verify health and route contract
Invoke-RestMethod http://localhost:8000/api/health
python scripts/verify_backend_routes.py

# Inspect live OpenAPI route paths
$o = Invoke-RestMethod http://localhost:8000/openapi.json
$o.paths.PSObject.Properties.Name | Sort-Object
```

### Operator QA checklist

Known-good local QA ports for the current web console:

- Backend: http://localhost:8001
- Frontend: http://localhost:5174

Before testing UI flows, verify the running backend is current source:

```powershell
python scripts/verify_backend_routes.py
Invoke-RestMethod http://localhost:8001/api/health
$o = Invoke-RestMethod http://localhost:8001/openapi.json
$o.paths.PSObject.Properties.Name | Sort-Object
Invoke-RestMethod http://localhost:5174/api/health
```

Operator smoke checklist:

| Area | Check |
| --- | --- |
| Batch management | Create a named QA batch, create an unnamed QA batch, rename it, switch batches from the dropdown, refresh, and delete only QA-created batches. |
| Upload/files | Upload one PDF and one unsupported test file; confirm file cards update, unsupported files are labeled cleanly, and dropped files do not navigate the browser. |
| Document workspace | Open a PDF, change pages, zoom, select the marking tool, choose a field label, add/delete a mark, collapse and expand the document pane. |
| Processing | Process an unsupported-only QA batch; confirm timeline stages skip cleanly and the run completes without raw errors. Use supported vendor batches only when Dropbox side effects are intended. |
| Template workspace | Verify Required, Issues, and All column views differ; search/filter rows; single-click selects a row; double-click edits a cell; Enter saves; Escape cancels; export uses edited rows. |
| Issues drawer | Open/close the drawer, select an issue, show the row, open its document, mark reviewed, switch batches, and confirm stale issues do not remain. |
| AI status | Confirm the pill says AI Off/Not Configured as appropriate, no keys are shown, and Configure AI remains a disabled placeholder. |
| Errors/toasts | Confirm success, warning, and error states are friendly and no operator-facing message contains raw JSON such as `{"detail":"..."}`. |

How to report UI bugs:

1. Note the clean backend/frontend ports being used.
2. Capture the batch id, visible action, expected result, and actual result.
3. Check whether the issue reproduces after restarting backend and frontend.
4. Include any browser console warning, but redact file paths, tokens, and customer-sensitive values.
5. Run `python scripts/verify_backend_routes.py` and mention whether it passed.

### Health check (without the frontend)

```bash
curl http://localhost:8000/api/health
# {"ok":true,"service":"billing_refactoring_2026_webapp"}
```

API docs (FastAPI swagger UI): http://localhost:8000/docs

## How to test with Richmond Utilities files

1. Open http://localhost:5173.
2. Drag the 14 sample CSVs from `Training Bills_Invoices/Water - Sewer/Richmond Utilities/Bills_Training/` into the drop zone.
3. The file list shows `richmond_utilities` (green badge, 95% confidence) for each file.
4. Click any file to preview its CSV content in the main panel.
5. Click **Process Batch**.
   - Backend stages files into `webapp_data/batches/<batch_id>/input/richmond_utilities/`.
   - Calls `process_richmond_utilities_batch(...)` with the batch-specific paths.
   - Returns 14 invoices, 16 ResMan rows, 9 flagged for manual review (matches CLI exactly).
6. The **ResMan template preview** shows all 16 rows with proper columns. Rows flagged for manual review are highlighted yellow; cells with missing required values are highlighted red.
7. The **Manual review** panel lists the 9 flagged invoices with their reasons (`unknown_unit_number_for_location`, `ambiguous_gl_mapping`, etc.).
8. (Optional) **Edit cells inline.** Click any cell to enter edit mode, type the new value, then press **Enter** to commit (or **Escape** to cancel). Edited cells get a green background and the header counter updates (e.g. `· 2 cells edited`). Click again to keep editing. Use **Reset Edits** to clear all overrides at once.
9. Click **Export Template**.
   - With no edits: copies the existing per-vendor workbook to `webapp_data/batches/<batch_id>/export/` (legacy behavior).
   - With edits: writes a fresh `resman_import_edited_<TS>.xlsx` from `Output/Template.xlsx`, applying the edited row data on top of the generated values.
10. Click **Download Excel** to download the latest export.

The original `Bills_Training` files are not modified. The original `Output/Template.xlsx` is not modified — the edited export reads it as a read-only stencil and writes a new copy.

### Edit semantics

- Edits are tracked per cell in the frontend (`{[rowIndex]: {[columnKey]: value}}`). Typing the original value back drops the edit, so the green highlight only shows real overrides.
- The Amount column and any cell whose original was numeric are coerced back to a number on commit, so totals stay accurate.
- **Re-processing or refreshing the preview discards edits.** Both buttons prompt with a `window.confirm` when there are unsaved edits.
- **Editing does not persist across page reloads.** Edits live in React state only — the backend gets them once, when you click Export Template.

## Endpoints (for direct API use or curl)

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/api/health` | Liveness |
| POST | `/api/batches` | Create batch (returns `batch_id`) |
| GET  | `/api/batches` | List existing batches |
| GET  | `/api/batches/{id}` | **Phase 1E**: batch status (`{batch_id, created_at, files, files_total, preview_available, export_available, export_filenames, summary}`). Returns 404 if the batch folder is gone. Used by the frontend to rehydrate after a page refresh. |
| GET  | `/api/batches/{id}/progress` | **Phase 1F**: live progress snapshot (`{status, percent, current_step, current_file, files_total, files_done, pages_total, pages_done, invoices_created, rows_created, warnings_count}`). Polled by the frontend every 750 ms while processing is active. |
| PATCH | `/api/batches/{id}` | **Phase 1G**: rename a batch (body: `{batch_name}`). |
| POST | `/api/batches` (with `{batch_name}`) | **Phase 1G**: create a named batch. |
| `POST /api/batches/{id}/process[?sync=1]` | **Phase 1G**: now returns 202 `{status: accepted, polling_url}` and runs the work in a background thread. Pass `?sync=1` for the legacy blocking behaviour. |
| GET  | `/api/batches/{id}/files` | List uploaded files + per-file vendor detection |
| POST | `/api/batches/{id}/upload` | Upload a single file (multipart `file=`) |
| GET  | `/api/batches/{id}/files/{filename}/preview` | Parsed CSV/XLSX as JSON |
| GET  | `/api/batches/{id}/files/{filename}/raw` | Raw stream (legacy; defaults to inline). |
| GET  | `/api/batches/{id}/files/{filename}/content` | Inline streaming with correct `Content-Type`, `Content-Disposition: inline`, and `X-Content-Type-Options: nosniff`. Used by the document preview iframe; path-traversal-safe. |
| POST | `/api/batches/{id}/detect` | Run vendor detection (also done implicitly by `/files`) |
| POST | `/api/batches/{id}/process` | Run vendor processor; cache result |
| GET  | `/api/batches/{id}/preview` | Generated ResMan rows as JSON |
| GET  | `/api/batches/{id}/manual-review` | Manual-review issues |
| POST | `/api/batches/{id}/export` | Copy generated xlsx to `export/`. Optional JSON body `{ "edited_rows": [...] }` writes a fresh template from edited row data instead. |
| GET  | `/api/batches/{id}/download` | Stream the latest export xlsx (or `?filename=...` for a specific one) |
| DELETE | `/api/batches/{id}` | Delete the batch folder |

## Workspace layout (Phase 1D)

Three CSS-grid columns:

```
+--------+----------------+----------------------------+
| 240 px |  360 px        |  remaining width           |
| Sidebar|  Doc preview   |  ResMan template           |
|        |  (collapsible) |  + manual-review drawer    |
+--------+----------------+----------------------------+
```

- **Sidebar** holds the (compact) drop zone, the file list, and the action buttons. The dropzone is marked `data-dropzone="true"` so the global drag/drop guard knows to LET drops through here (and swallow them everywhere else).
- **Document preview column** is collapsible. Click *Collapse* in its header to shrink it to a 36 px vertical rail; click the rail to expand. It uses the new `/content` endpoint with `Content-Disposition: inline` so PDFs render in an iframe instead of triggering a download.
- **Template column** is the primary workspace. The ResMan grid stretches to fill the column; the manual-review drawer takes a max of ~38vh at the bottom and has its own collapse toggle.

Below 1100 px viewport the document preview column hides automatically; below 760 px all three columns stack.

### Drag/drop bug fix (Phase 1D)

A window-level `useEffect` in `App.tsx` calls `preventDefault()` on `dragenter` / `dragover` / `drop` whenever the drag carries files AND the target isn't inside an element marked `data-dropzone="true"`. This prevents Chrome from navigating away from the app when a user accidentally drops a PDF outside the drop zone. The handler is a no-op for non-file drags (e.g. selected text), and listeners are cleaned up on unmount.

## Persistence across page refresh (Phase 1E)

The active `batch_id` is cached in `localStorage` under the key `billing_refactoring_active_batch_id`. On app load the frontend calls `GET /api/batches/{id}` to verify the batch still exists; if it does, it re-fetches the file list, the ResMan preview, and manual review. If the backend returns 404 (folder deleted, server reset, etc.) the localStorage entry is cleared automatically and the app starts on a blank workspace. **Clear Batch** wipes both the server-side folder and the localStorage entry.

## Full template preview + collapsible optional columns (Phase 1E)

The preview now shows every column declared in `Output/Template.xlsx` (24 columns for the current ResMan AP template), padded with `null` where the vendor processor doesn't populate a value. Three categories drive UI styling:

| Category | Source | Header style | UI behavior |
| --- | --- | --- | --- |
| **required** | `config/resman_template_rules.yaml` → `required_columns` (12 fields) | Orange `#ffd6a8` with a `*` marker | Always visible. Cells with empty values get a red error tint. |
| **recommended** | YAML → `recommended_columns` (3 fields) | Soft amber | Always visible. |
| **optional** | Implicit (anything in the template not classified above) | Neutral | Hidden by default; one-click toggle in the template header to reveal them. Hidden columns still flow into the export. |

Edit any cell directly (Phase 1B inline editing); edits to optional cells are preserved even if the column is later hidden.

## One-click Export (Phase 1E)

The primary action is **Export & Download** — a single click that POSTs to `/api/batches/{id}/export` (with edited rows if the operator edited any cells), then triggers an anchor-click download of the generated xlsx. The old "Download Excel" button is repurposed as **Re-download last export** for when the operator wants to grab the previous file again without rebuilding it.

## Docker (Phase 1E)

Boot the whole stack with one command:

```bash
docker compose up --build
# backend  → http://localhost:8000   (FastAPI + /api/health + /docs)
# frontend → http://localhost:5173   (Vite proxy → backend:8000)
```

`webapp_data/` is bind-mounted from the host so generated batches survive container restarts. The backend image ships with Tesseract + Poppler so scanned PDFs work out of the box. See [DOCKER_WEBAPP_README.md](../docs/DOCKER_WEBAPP_README.md) for the full guide (ports, environment, troubleshooting, production-style serving).

## Supported vendors

| Vendor | Detector signal | Status |
| --- | --- | --- |
| **Richmond Utilities** | Filename `<digits>_<digits>_BillingHistory*.csv`, CSV header `Transaction/Service/Meter Number`, PDF filename contains "richmond", or PDF text-layer mentions "Richmond Utilities" / "richmondutilities.com" | Phase 1A onwards |
| **Hopkinsville Water Environment Authority (HWEA)** | PDF text-layer mentions "Hopkinsville Water Environment Authority" / "hwea-ky.com" / "(270) 887-4246" — or filename contains `HWEA` / `hopkinsville` for scanned PDFs. PDFs whose text says "City of Henderson" (misfiled bills in the HWEA folder) are explicitly rejected. | **Phase 1F** |
| Other vendors | — | `unknown` (manual override pending) |

Detection on PDFs is intentionally cheap — we do **not** OCR during detection (it's a UI hot path). Scanned PDFs without a filename hint will return `unknown`; the operator can rename the file or route it manually. Once routed to a supported processor, scanned PDFs are OCR'd as part of `Process Batch`.

Future phases extend `_DETECTORS` in `webapp/backend/services/vendor_detection.py` to add more vendors. Each supported vendor key is registered in `webapp/backend/services/batch_processor.py` as `(loader_function, entrypoint_name)` so adding more vendors is one line.

## Progress bar (Phase 1F)

When the operator clicks **Process Batch**, the sidebar renders a small progress card next to the action button. The card shows:

- The current step (e.g. `Reading UtilityBill_04_2026 (1).pdf`, `OCR page 7 of 14`, `Uploading split PDF for 0036-28653-003 Apr 26…`, `Building ResMan preview`, `Done`).
- A percent bar (`active` blue, `completed` green, `failed` red).
- Aggregated counts: files done / total, pages done / total, invoices created, rows created, warnings flagged.

Implementation:
- Backend: `utils/progress_tracker.ProgressTracker` writes an atomic JSON snapshot to `webapp_data/batches/<id>/progress.json` on every update. The webapp's `process_batch` instantiates a tracker per batch and passes a `progress_callback` to whichever vendor processor accepts the kwarg (introspected via `inspect.signature`).
- Endpoint: `GET /api/batches/{id}/progress` reads the snapshot and returns it as JSON. Returns `status=idle` if the batch hasn't started processing yet.
- Frontend: `App.tsx` polls the endpoint every 750 ms via `setInterval` while `isProcessing` is true and stops polling when `status` becomes `completed` or `failed`. Listeners are cleaned up on unmount.

Old vendor processors (Richmond Utilities pre-Phase-1F) keep working unchanged — the webapp introspects the function signature and only passes `progress_callback` to processors that declare it.

## Richmond Utilities PDF processing

When a PDF detected as Richmond Utilities is uploaded:

1. **Process Batch** triggers the backend processor, which:
   - Reads the PDF text layer (digital PDFs) or runs OCR (scanned PDFs).
   - Parses each page into one ResMan invoice (account number, billing date, due date, service period, address, line items).
   - Cross-checks `sum(line items) == TOTAL DUE NOW`. Mismatches are flagged.
   - **Phase 1D**: splits the PDF into one 1-page support PDF per bill (`Richmond_Utilities_<account>_<Mon>_<YY>.pdf`) and uploads each one to Dropbox separately under `…/split_bills/`. Each ResMan row gets the URL of the specific bill it came from.
   - If the per-bill split fails for a given page, the row falls back to the link of the full original PDF and is flagged `support_pdf_split_failed`.
2. The preview shows the new invoice rows alongside any CSV-derived rows. PDF rows now have row-specific `Document Url` values.
3. The manual-review panel lists every page with extraction issues (`extracted_total_mismatch`, `unknown_unit_number_for_location`, `support_pdf_split_failed`, …).
4. Use the Phase 1B **inline cell editing** to correct OCR artifacts on the Amount, Description, or Location cells before clicking Export Edited Template.

OCR setup (one-time, only needed for *scanned* PDFs): see [Richmond Utilities README](../Training%20Bills_Invoices/Water%20-%20Sewer/Richmond%20Utilities/README_RICHMOND_UTILITIES.md#first-time-setup) for Tesseract + Poppler installation. Without OCR, scanned PDFs are flagged `ocr_required_but_unavailable` but CSVs and digital PDFs still process normally.

`pypdf` (already in the project's dependencies) handles the per-bill split. If `pypdf` is missing, every PDF page falls back to the full-PDF link and is flagged `support_pdf_split_failed` — the rest of the batch still completes.

## Phase 1H — Premium UI, batch document modes, AI fallback skeleton, PDF workspace

Phase 1H lays the foundation for a premium, property-manager-friendly workspace without changing how Richmond / Hopkinsville actually process bills. Five additions:

1. **Batch document mode.** When you click "+ New batch", a modal asks "What type of documents are you uploading?" with a card grid: **Auto-detect** (default), Digital PDFs, Scanned PDFs, Mixed PDFs, CSV / Excel. The pick is stored in `batch_metadata.json` (`document_mode`) and flows into `run_context["document_mode"]` for vendor processors. CLI runs are unaffected.
2. **Processing timeline.** The progress bar gains an expandable timeline below it that reads `progress.stages[]` from the per-batch JSON. Each stage (Upload, Vendor detect, Reading PDF, OCR, YAML rules, Address match, Unit Info Clean match, GL evidence, AI fallback, Reconcile, Split PDFs, Dropbox, Build template, Ready) shows a status icon (pending / running / completed / warning / failed / skipped) and a duration once it finishes. Old vendor processors keep working — when no stages are declared, the timeline simply doesn't render.
3. **AI fallback skeleton (disabled by default).** A new service at `webapp/backend/services/ai_fallback.py` plus `config/ai_fallback_rules.yaml`. Behaviour is governed by env vars (`AI_FALLBACK_ENABLED`, `AI_PROVIDER`, `<PROVIDER>_API_KEY`) plus the YAML's `enabled:` flag — both must agree. Phase 1H ships only the `DisabledAdapter`; the four provider stubs (`openai`, `anthropic`, `google_gemini`, `deepseek`) raise `AIProviderNotImplementedError` so a misconfiguration cannot trigger an external request. The topbar shows an "AI" pill — green when ready, neutral when off, with the reason in the tooltip. AI never overrides Unit Info Clean validation, GL evidence, or bill-total reconciliation; every AI-filled field would be flagged for manual review.
4. **PDF workspace (Field Region Mode).** The Document Preview header now has a **Native / Field regions** toggle. Native is the existing `<iframe>` viewer (default). Field regions opens a PDF.js canvas + HTML overlay where you can:
   - Switch tools (Select / Draw / Pan / Delete).
   - Pick a region label (service_address, account_number, invoice_date, due_date, total_amount, line_items, notice_block, ignore_zone, custom).
   - Draw a rectangle with the mouse — coordinates are stored **normalized to [0,1]** so they survive zoom changes and screen resizes.
   - Move regions with the Select tool, resize from the four corner handles, delete with the × chip or the Delete tool.
   - Page-nav arrows + zoom in/out (50%–300%).
   Regions persist via `PUT /api/batches/{id}/regions` to `webapp_data/batches/<id>/region_hints.json`. The PDF.js bundle is **lazy-loaded** so the native preview path doesn't pay for it.
5. **Premium polish.** Pills, modal dialog with backdrop fade and card pop, mode-card grid, segmented mode toggle in the preview header, timeline rows with status colours and a soft pulse on the running stage, region handles + delete chip on the workspace, skeleton-loader keyframes for any future lazy panels.

### Environment variables

```dotenv
# Existing — Dropbox (unchanged)
DROPBOX_APP_KEY=...
DROPBOX_APP_SECRET=...
DROPBOX_REFRESH_TOKEN=...
DROPBOX_BASE_FOLDER=/Billing_Refactoring_2026

# Phase 1H — AI fallback (disabled by default)
AI_FALLBACK_ENABLED=false
AI_PROVIDER=disabled                 # disabled | openai | anthropic | google_gemini | deepseek
# OPENAI_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...
# GOOGLE_API_KEY=AIza...
# DEEPSEEK_API_KEY=...
```

`/api/ai/status` exposes only `enabled / provider / configured / reason / policy / max_cost_per_batch_usd / allowed_tasks` — never an API key.

### New endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/ai/status` | Operator-safe AI fallback metadata (no keys). |
| GET | `/api/batches/{id}/regions` | List all region hints in the batch. |
| PUT | `/api/batches/{id}/regions` | Replace the whole region list. |
| POST | `/api/batches/{id}/regions` | Append (or upsert by `id`) a single region. |
| DELETE | `/api/batches/{id}/regions/{region_id}` | Remove one region. |

`POST /api/batches` and `PATCH /api/batches/{id}` now accept `document_mode`, `ai_fallback_enabled`, `ai_fallback_policy`. All have safe defaults; older clients keep working.

### What does NOT happen yet
- No real provider call — the four real adapters are typed stubs.
- Vendor processors don't yet consume `run_context["region_hints"]` — the data is wired through but Richmond / Hopkinsville don't use it yet. (Foundation only.)

For full architecture details see [WEBAPP_PREMIUM_AI_PDF_WORKSPACE_PLAN.md](../WEBAPP_PREMIUM_AI_PDF_WORKSPACE_PLAN.md). For the implementation report and test results see [WEBAPP_PHASE_1H_PREMIUM_AI_WORKSPACE_REPORT.md](../WEBAPP_PHASE_1H_PREMIUM_AI_WORKSPACE_REPORT.md).

## Phase 1J — Premium workspace UX

Phase 1J is a UX/layout overhaul. Vendor processing logic is unchanged; CLI / Dropbox / export behave exactly as before. AI remains disabled by default. The visible product changes:

### New layout

```
┌─ Topbar (52 px) ───────────────────────────────────────────────────────────┐
│ Brand  │  Upload → Process → Review → Export    │  AI pill   Batch picker │
├─ Nav rail ┬─ File sidebar ┬─ Document pane ┬─ Template area · Inspector ─┤
│  📦 active │  Actions      │  Document /   │  Summary bar                 │
│  ✅ Soon   │  Progress     │   Mark Fields │  Required / Review / Full   │
│  📐 Soon   │  Timeline     │  PDF / CSV    │  Search · Row filter        │
│  ↓  Soon   │  Drop zone    │   preview     │  Editable grid              │
│  ⚙ Soon    │  File list    │               │  Issues / Selected row tabs │
└────────────┴───────────────┴───────────────┴─────────────────────────────┘
```

### Resizable panels
Three vertical splitters (file sidebar / document pane / inspector pane) drag-to-resize via [`useResizablePanel`](frontend/src/hooks/useResizablePanel.ts). Sizes persist per browser:

| localStorage key | Default | Min – Max |
| --- | ---: | --- |
| `billing_refactoring_layout_sidebar_width` | 280 px | 220–460 |
| `billing_refactoring_layout_document_width` | 480 px | 320–720 |
| `billing_refactoring_layout_inspector_width` | 360 px | 260–560 |

Double-click any divider to reset to default. Document and inspector panes can also be collapsed to a 36 px rail with a click.

### Compact action bar
Replaces the older stacked-button column. Lives at the top of the file sidebar:

- **▶ Process** — primary action, disabled until files are uploaded; shows an inline spinner during processing.
- **↓ Export** — emphasised once a preview exists; includes the unsaved edit count when relevant.
- **More ⋯** — dropdown for Refresh preview · Reset edits · Re-download last export · Delete batch.

### Workflow steps (topbar)
A 4-step indicator: Upload → Process → Review → Export. Each step shows live state ("3 files", "12 invoices", "3 issues", "Ready") and a status colour (pending / active / complete / warning).

### AI status pill
Click the pill to open a popover. Possible labels: **AI Off** (default), **AI Not Configured**, **AI Ready**, **AI Error**, **AI…** (loading). The popover shows provider, policy, cost ceiling, what AI would help with, and a hint to configure `.env`. **No API keys are ever displayed.** No real provider call happens in this phase.

### Field region workflow
The mode toggle in the document panel header reads **Document / Mark Fields** (the implementation-detail labels "Native" and "Field regions" are gone). Switch to **Mark Fields** to draw labelled rectangles on a PDF page; coordinates are normalized 0–1 and persist via `PUT /api/batches/{id}/regions`. The PDF.js workspace lazy-loads only when this mode is opened, so the **Document** view stays cheap.

If `region_hints.json` doesn't exist yet, the workspace shows a clean empty state — no more raw `404 Not Found` errors. Real server errors render a one-line message + **Retry** button.

### Template review workspace
The grid now lives inside [`TemplateWorkspace`](frontend/src/components/TemplateWorkspace.tsx) with:

- **Summary bar**: Files / Invoices / Rows / Flagged / Edited / Missing link / Total.
- **View presets**: *Required* (required + recommended only) · *Review* (adds Document Url + Reference + Description) · *Full template* (every column).
- **Search**: matches Invoice Number / Vendor / Property / Location / Service address / Description.
- **Row filters**: Needs review · Edited · Missing property · Missing location · Amount mismatch · Missing link.
- **Row selection**: clicking any row highlights it and switches the inspector to **Selected row**.

### Review / Inspector panel
Replaces the old bottom-of-screen Manual Review table. Two tabs:

- **Issues** — issue cards grouped by source file, each card with a severity dot, human explanation, meta pills (property / amount), and **Show row** / **Open document** actions.
- **Selected row** — property-list view of every important field, an **Open support document** button (when `Document Url` is present), a **Provenance** section (match strategy / confidence / period source), and a **Manual review** section with help text per reason.

### Export workflow
Unchanged backend. The compact bar's Export button drives the same `/api/batches/{id}/export` flow with the editable preview state. Document Url, full Template.xlsx column set, and Dropbox links are all preserved.

### Backend metadata
Single change in this phase: `GET /api/batches/{id}/regions` returns 200 + empty list when the batch directory is missing (instead of 404). Eliminates the noisy region error in the workspace when localStorage points to a deleted batch.

For full details and tests, see [WEBAPP_PHASE_1J_PREMIUM_WORKSPACE_UX_REPORT.md](../WEBAPP_PHASE_1J_PREMIUM_WORKSPACE_UX_REPORT.md).

## Phase 1K — Visual system + workspace refinement

Phase 1K refines the look and feel without changing the workflow Phase 1J introduced. Vendor processing, CLI, Dropbox, export, and AI safety guarantees are unchanged. Highlights:

### Toasts replace persistent banners
Status messages (batch restored, processed N files, exported, renamed, switched, etc.) now appear as compact bottom-right **toasts** instead of a column-eating green panel. They auto-dismiss after 4 s (6 s for processing summaries) and stack so back-to-back events aren't lost.

### View presets (in topbar)
Three workspace presets, persisted to `localStorage["billing_refactoring_layout_view_preset"]`:

| Preset | Effect |
| --- | --- |
| **Review** *(default)* | Document + Template + Inspector all visible |
| **Template** | Doc and Inspector collapsed; the template grid takes the whole screen |
| **Document** | Document marking — wider PDF workspace |

The actual collapsing flows through the same collapse states the Phase 1J rail buttons control; resizable widths still persist independently.

### Mark reviewed (browser-session)
Each issue card in the Review / Inspector panel now has a **Mark reviewed** action. Reviewed cards are visibly de-emphasised so the operator can see what they've already worked through. **The state is browser-session only** — reloading clears it, and the underlying `manual_review_reasons` value is never modified.

### Friendlier copy
- Region tags display **Service address / Account number / Total amount / …** (snake_case stays internally).
- Vendor pills show **Richmond / Hopkinsville** instead of full snake_case keys.
- Disabled nav rail items lost their visible "Soon" badge — they're still present (slightly muted) and the tooltip explains "coming later".
- The verbose "Click any cell to edit…" banner above the grid is gone.

### File-type badges
Each file row in the sidebar now carries a small per-type badge (PDF / CSV / XLSX / Image) with a soft tone matching the type. Selected file gets a 2-px accent stripe on the inner left edge.

### Document Url
Long URLs no longer steal column width. Each row shows a compact **↗ Open** chip (or a muted `—` when no URL is present).

### AI status meanings
The AI pill never says **AI Error** unless the `/api/ai/status` request actually fails. Possible labels (and what they mean):

| Label | Meaning |
| --- | --- |
| **AI Off** | `provider=disabled` (default). App runs rules + OCR + YAML only. |
| **AI Not Configured** | Provider chosen, but no API key. |
| **AI Ready** | Master switch on, key present. AI may suggest values when rules confidence is low. |
| **AI Error** | The status fetch itself failed. |
| **AI…** | Loading. |

Click the pill to open a popover with provider, policy, cost ceiling, and what AI would help with. **No API keys are ever returned or displayed.** No real provider call happens in this phase.

### Visual system at a glance
Refined CSS variables (warmer neutral background `#f3f5f8`, softer borders `#d8dee4` / `#eef0f3`, slightly cooler accent `#2563eb`), declared spacing scale (`--space-1` through `--space-5`), declared radius scale (`--radius-sm` through `--radius-lg`), three shadow tiers, antialiased fonts at 13 px base.

For full details and tests, see [WEBAPP_PHASE_1K_VISUAL_SYSTEM_REFINEMENT_REPORT.md](../WEBAPP_PHASE_1K_VISUAL_SYSTEM_REFINEMENT_REPORT.md).

## Phase 1L — Product UI simplification

Phase 1L removes visual noise so the app stops feeling like an admin console. Backend, processors, CLI, and AI safety guarantees are unchanged. Highlights:

### Issues drawer (replaces the fixed right inspector pane)
The right-side review panel that used to claim a permanent column is gone. In its place:

- A small **Issues** pill in the topbar shows the current count: **N issues** (warn/error tone) or **No issues** (clean, green).
- Clicking the pill slides a right-side **drawer** in over the workspace. The drawer reuses the same issue cards and per-issue **Mark reviewed** behaviour from Phase 1K.
- The drawer closes via the X icon, **Escape**, or clicking the dimmed backdrop. It overlays — never steals width from — the template grid.

### No more visible "Collapse" / "Expand" labels
Every panel collapse button is now a **chevron icon button** with a `title=` tooltip ("Collapse panel" / "Expand panel"). The text labels are gone.

### Compact workflow strip
The 1-2-3-4 numbered circles were replaced with a single rounded **status strip**:

```
●  Upload · 4 files  ›  ●  Process · 12 invoices  ›  ▲  Review · 3 issues  ›  ●  Export · Ready
```

Each step has a coloured dot, a label, and a small detail. The whole strip is informational — clicks don't navigate.

### View presets
The topbar segmented switcher dropped its "VIEW" prefix and renamed the presets:

| Preset | Effect |
| --- | --- |
| **Review** *(default)* | Document and Template both visible |
| **Template focus** | Document collapsed; Template fills the screen |
| **Document focus** | Wider document workspace for marking |

The Issues drawer is a separate explicit action — opening / closing it is no longer tied to the preset.

### AI status meanings
The AI pill now distinguishes deployment-config issues from runtime failures so the operator never sees a misleading "AI Error":

| Pill | Meaning |
| --- | --- |
| **AI Off** *(default)* | `provider=disabled`, OR the `/api/ai/status` request itself didn't reach the backend. App runs rules + OCR + YAML only. |
| **AI Not Configured** | A provider is selected but no API key is set yet. |
| **AI Ready** | Master switch on, key present. AI may suggest values when rules confidence is low. |
| **AI Error** | A real provider runtime error occurred. *(Currently unreachable — no real provider calls fire in this phase.)* |
| **AI…** | Loading. |

The popover keeps a friendly first sentence ("AI assist is currently off…"); `.env` configuration details live in a collapsed `Configuration` section so they don't dominate the popover.

### Document marks terminology
Region labels (Service address / Account number / Total amount / etc.) display in friendly form throughout the UI; backend keys remain snake_case. The mode toggle reads **Document / Mark** (no more "Native" / "Field regions").

### Resizable splitter bug fix
Dragging the divider between the document and template panes used to occasionally keep resizing after the mouse was released. Phase 1L rewrites the resize hook to use Pointer Events + `setPointerCapture` + an `e.buttons === 0` hard guard + window-level safety nets for `blur` / `visibilitychange` / `mouseleave`. See [WEBAPP_PHASE_1L_RESIZER_BUGFIX_REPORT.md](../WEBAPP_PHASE_1L_RESIZER_BUGFIX_REPORT.md) for full root cause and tests.

### Mouse / keyboard / pointer behaviour summary
- **Drag a divider:** hold left mouse button down, move pointer, release to commit. Double-click a divider to reset its panel to the default size.
- **Open Issues:** click the topbar pill. Escape closes.
- **Mark an issue reviewed:** click **Mark reviewed** inside an issue card (browser-session only — refresh clears it).
- **Switch view preset:** click Review / Template focus / Document focus in the topbar.

For full details and tests, see [WEBAPP_PHASE_1L_PRODUCT_UI_SIMPLIFICATION_REPORT.md](../WEBAPP_PHASE_1L_PRODUCT_UI_SIMPLIFICATION_REPORT.md).

## Phase 1N — Processing Control + Interaction Cleanup

Phase 1N adds Stop / Cancel for active runs, fixes click-to-edit, simplifies the topbar, and refines AI / column-view copy. The CLI and vendor processors stay byte-identical when invoked without the new optional kwargs.

### Stopping a running batch
- Click **Process** to kick off a run.
- While processing, a **Stop** button appears next to the Process button (`btn-danger` style).
- Clicking Stop confirms ("Stop processing this batch?") and posts to `POST /api/batches/{id}/cancel`.
- The vendor processor checks for cancellation **between files**. The current file finishes, then the run halts. No threads are killed forcibly; partial output stays consistent.
- The button label switches to **Cancelling…** while the worker drains. Once stopped, status flips to `cancelled` and the operator can re-run.
- A "Processing cancelled" toast surfaces; partial preview (if any) is loaded so the operator can inspect what completed.

### Table edit behaviour
- **Single click** on a row selects it (highlights in accent-soft, drives the inspector if open).
- **Double click** on a cell opens its editor.
- Edit shortcuts unchanged: **Enter** commits, **Escape** cancels, **blur** commits.
- Edited cells keep the green outline; export still uses edited values.

### Loading & batch switching
- Switching batches from the sidebar surfaces a brief **"Loading batch…"** toast so the operator sees something is happening before the new payload paints.
- Document preview loading still uses the lazy-loaded PDF.js workspace; render tasks are cancelled when the file changes (Phase 1H carry-over).
- Polling status terminal states now include `cancelled` alongside `completed` / `failed`.

### Column views
| Button | What it shows | Tooltip |
| --- | --- | --- |
| **Required** | Required + recommended columns only | "The core fields needed for ResMan import." |
| **Issues** | Required + recommended + Document Url + Reference + Description | "Most useful for fixing flagged rows." |
| **All** | Every column from `Output/Template.xlsx` | "Every column from the official ResMan Template.xlsx." |

The label `Columns:` sits in front of the segmented buttons so the affordance is obvious.

### AI status (refined copy)
The popover leads with a friendly message — *"AI assist is currently off. The app is using rules, OCR, YAML, and validation only."* — followed by `Status / Provider / Mode` rows, the allowed-tasks list, a disabled **Configure AI** button (placeholder for a future Settings menu), and a collapsed *Developer setup* `<details>` block with the `.env` instructions for the technical user. **No API keys** are returned by `/api/ai/status` or shown anywhere in the UI.

### Topbar simplification
The "Review · Template focus · Document focus" segmented switcher was removed from the topbar. Panel collapse buttons + Process/Stop/Export/Issues already cover the same ground. The internal preset state is preserved so a future Settings menu can resurrect the switcher.

### Brand mark
The `BR` text block in the nav rail was replaced with a small **bill** icon (document outline + lines). Reads as "billing app" without needing wordmark text.

### New backend endpoint
`POST /api/batches/{batch_id}/cancel` →

| Result | Status |
| --- | --- |
| Tracker registered (active run) | 200 + `{status: "cancelling"}` |
| No active run for this batch | 200 + `{status: "no_active_run"}` |
| Batch directory missing | 404 |

For full details and tests, see [`docs/reports/phases/WEBAPP_PHASE_1N_PROCESSING_CONTROL_RENDERING_STABILITY_REPORT.md`](../docs/reports/phases/WEBAPP_PHASE_1N_PROCESSING_CONTROL_RENDERING_STABILITY_REPORT.md).

## Phase 1O — Smooth progress + stable document rendering

Phase 1O fixes two perceived-performance bugs:

### Progress bar no longer freezes at 5 %
The OCR loop in `utils/pdf_text_extractor.py` now emits a per-page progress callback. The Hopkinsville processor wires this into a `_ocr_progress` closure that maps `(done, total, label)` into the first half of the file's percent slice; the per-page parser claims the second half. A 14-page scanned PDF that previously sat at 5 % until OCR finished now moves smoothly through OCR (≈3–5 s per page → one bar update per page) into parse → into completion. The progress label reads:

```
2629 KENWOOD DR.pdf — Rasterising for OCR…
2629 KENWOOD DR.pdf — OCR page 4 of 14…
2629 KENWOOD DR.pdf — OCR page 4 of 14 done
…
Parsing 2629 KENWOOD DR.pdf — page 4 of 14
```

The frontend's progress polling cadence dropped from 750 ms to 500 ms so the visible bar moves with each backend tick.

### Document workspace no longer flickers
`PdfPageCanvas` was rewritten:

- **Stable callback ref**: `onPageRendered` is held in a `useRef` instead of a dependency, so unrelated parent re-renders (toasts, progress polls, issue updates) never restart the canvas effect.
- **Per-tab document cache**: opening different pages of the same PDF reuses the parsed pdf.js document. Cap: 4 most-recent documents.
- **Offscreen render + atomic blit**: each new frame paints into a hidden canvas first; only when it's fully painted is the visible canvas resized and the buffer drawn onto it. No white flash.
- **Delayed polished overlay**: the loading state appears only after a 250 ms threshold (so fast page navs never flash) and uses a translucent + backdrop-blur layer with a dot-pulse animation and the friendly label *"Loading document…"*. The raw `Rendering…` text is gone.
- **Cancelled renders are honored**: switching files / pages mid-render cleanly stops the in-flight pdf.js task without painting stale output.

For full root cause and tests, see [`docs/reports/phases/WEBAPP_PHASE_1O_SMOOTH_PROGRESS_AND_RENDERING_REPORT.md`](../docs/reports/phases/WEBAPP_PHASE_1O_SMOOTH_PROGRESS_AND_RENDERING_REPORT.md).

## Phase 1P — Batch management UX + action placement

### Creating a batch
Click **+ New batch** in the file sidebar. The modal asks for an optional name and the document mode (Auto-detect / Digital / Scanned / Mixed / CSV-Excel). The supplied name is persisted in `webapp_data/batches/<batch_id>/batch_metadata.json` and surfaces immediately in the sidebar header and the recent-batches dropdown.

### Renaming a batch
Click the dots menu in the file sidebar's batch header → **Rename batch**. An app-native modal prefills with the current name; **Enter** saves, **Escape** cancels. Empty / over-80-character names show an inline red error in the field. Backend errors (404 / 400) surface in the same inline error label — no `window.prompt`, no giant red workspace banner.

The backend endpoint is `PATCH /api/batches/{batch_id}` with body `{"batch_name": "..."}`. If you ever see an HTTP 405 in the dev console, restart the FastAPI backend — the PATCH route was added in Phase 1H, and a stale running backend won't have it.

### Where actions live
- **Process** + **Stop** + **More** menu (Refresh preview / Reset edits / Re-download last export / Delete batch): file sidebar action bar.
- **Export**: template workspace summary bar (right edge, next to the Total). Visible only once a preview has rows. The button reads `Export` or `Export (N)` when there are unsaved edits.

This split mirrors the conceptual layout: **Process** acts on the uploaded files (file sidebar); **Export** acts on the template (template workspace).

### Error surfacing
Rename / switch-batch / create-batch / processing failures now surface as **toast notifications** (bottom-right). Detailed error text goes to the browser console for developers. A persistent banner is reserved for two cases — backend unreachable on app start and a preview that couldn't be restored after a refresh.

### Custom names — verified
Names typed into the modals end up in `batch_metadata.json` and surface in:
- the file sidebar batch header (primary line)
- the batch dropdown (primary line)
- toast confirmations after rename / create

If a metadata file is missing the `batch_name` field (legacy batches from very early phases), the UI falls back to the `batch_id` so nothing is unlabelled.

For full root cause + tests, see [`docs/reports/phases/WEBAPP_PHASE_1P_BATCH_MANAGEMENT_UX_REPORT.md`](../docs/reports/phases/WEBAPP_PHASE_1P_BATCH_MANAGEMENT_UX_REPORT.md).

## Things this Phase **does NOT** do

- ❌ Authentication
- ❌ Database
- ❌ Background processing / progress bars (processing is synchronous; ~5–10 s for 14 CSVs, +60–80 s when OCR'ing a 14-page scanned PDF)
- ❌ Per-PDF re-OCR with operator-selected DPI / preprocessing
- ❌ Word document support
- ❌ Persisted edits (cell edits live in React state only; reload or re-process discards them)
- ❌ Bulk edit / find-and-replace across cells
- ❌ Multi-vendor processing (Richmond Utilities only — CSV, XLSX, and PDF)

## Limitations / known issues

- Richmond Utilities is the only wired-up processor. Files detected as `unknown` are listed but not processed; they appear in the `unsupported_files` array on the `/process` response.
- File preview for `.docx` only shows metadata; render is up to the frontend (currently a "not supported" message). PDF/image use `<embed>` / `<img>` against the `/raw` endpoint.
- The processing endpoint is synchronous. For batches >50 files this will block the HTTP request.
- No inline error recovery: if a single file inside a batch fails, the whole batch run still continues but the file ends up in `unsupported_files` or the per-file manual-review entry.

## Where the data goes

| Path | Purpose |
| --- | --- |
| `webapp_data/batches/<batch_id>/input/<file>` | Original upload, never modified after the upload |
| `webapp_data/batches/<batch_id>/input/<vendor>/<file>` | Vendor-specific staging copy passed into the processor |
| `webapp_data/batches/<batch_id>/processed/<vendor>/*` | Generated workbook + manual review + debug CSV + log |
| `webapp_data/batches/<batch_id>/processed/_webapp_result.json` | Cached preview/manual-review JSON |
| `webapp_data/batches/<batch_id>/export/*.xlsx` | Final downloadable Excel |

`webapp_data/` is gitignored.

## Why nothing here breaks the CLI

`process_richmond_utilities.py` was refactored to expose a callable function:

```python
def process_richmond_utilities_batch(
    input_folder: Path | None = None,
    output_folder: Path | None = None,
    template_path: Path | None = None,
    config_path: Path | None = None,
    run_context: dict | None = None,
) -> ProcessBatchResult: ...
```

When called with **no arguments** it falls back to the original module-level paths (`TRAINING_FOLDER`, `OUTPUT_FOLDER`, etc.) and behaves exactly like before. `main()` is now a one-line CLI wrapper around it. The webapp passes batch-specific paths.

Verified: running `python "Training Bills_Invoices/Water - Sewer/Richmond Utilities/process_richmond_utilities.py"` after the refactor still produces 14 invoices / 16 line items / 9 flagged — identical to the pre-webapp behavior.
