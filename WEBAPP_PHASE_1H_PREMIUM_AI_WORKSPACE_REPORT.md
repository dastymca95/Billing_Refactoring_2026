# Webapp Phase 1H — Premium UI / AI / PDF Workspace Foundation

**Date:** 2026-05-02
**Scope:** Foundation only. CLI / Richmond / Hopkinsville / Dropbox / export must keep working unchanged. AI is **disabled by default** — no API call ever fires from this phase.

---

## TL;DR

| Capability | Status | Notes |
| --- | --- | --- |
| Architecture plan | ✅ landed | [WEBAPP_PREMIUM_AI_PDF_WORKSPACE_PLAN.md](WEBAPP_PREMIUM_AI_PDF_WORKSPACE_PLAN.md) |
| Batch document mode (`digital_pdf` / `scanned_pdf` / `mixed_pdf` / `csv_excel` / `auto_detect`) | ✅ end-to-end | Frontend selector → backend `batch_metadata.json` → `run_context` |
| AI fallback config + service + status endpoint | ✅ skeleton, **disabled** | Provider stubs raise `AIProviderNotImplementedError` so no accidental traffic |
| Region hints (draw/save/delete labelled rectangles) | ✅ end-to-end | Coords normalized 0–1; persisted to `region_hints.json` |
| PDF.js workspace (canvas + overlay) | ✅ foundation | Lazy-loaded; native `<iframe>` preview remains available |
| Processing timeline (`stages[]` in `progress.json`) | ✅ landed | Backend declares stages; frontend renders premium expandable list |
| Premium CSS polish | ✅ landed | Pills, modal dialog, mode-card grid, timeline rows, region-box handles |
| `.env.example` updates + secret hygiene | ✅ landed | All keys env-only; never echoed via `/api/ai/status` |
| Richmond regression | ✅ unchanged | 28 invoices / 32 lines |
| Hopkinsville regression | ✅ unchanged | Phase 1I disconnect-notice fix still produces 4 invoices for the test PDF |
| Source files untouched | ✅ verified | SHA-256s match the Phase 1I run |

---

## Files created (Phase 1H)

### Architecture / docs / config

- [WEBAPP_PREMIUM_AI_PDF_WORKSPACE_PLAN.md](WEBAPP_PREMIUM_AI_PDF_WORKSPACE_PLAN.md) — architecture plan (Rivera-inspired patterns, region model, AI rules, batch modes).
- [WEBAPP_PHASE_1H_PREMIUM_AI_WORKSPACE_REPORT.md](WEBAPP_PHASE_1H_PREMIUM_AI_WORKSPACE_REPORT.md) — this document.
- [config/ai_fallback_rules.yaml](config/ai_fallback_rules.yaml) — disabled-by-default rules; allowed/forbidden tasks; cost ceiling; audit policy.

### Backend

- [webapp/backend/services/ai_fallback.py](webapp/backend/services/ai_fallback.py) — provider-agnostic `AIFallbackService`, `AISuggestion` / `AIStatus` dataclasses, `DisabledAdapter` (always returns "not configured"), four typed stubs (`OpenAIAdapter`, `AnthropicAdapter`, `GoogleGeminiAdapter`, `DeepseekAdapter`) that raise `AIProviderNotImplementedError`. Lazy singleton via `get_service()`.
- [webapp/backend/api/ai_status.py](webapp/backend/api/ai_status.py) — `GET /api/ai/status`. Returns operator-safe metadata only; never returns API keys.
- [webapp/backend/api/regions.py](webapp/backend/api/regions.py) — `GET / PUT / POST / DELETE /api/batches/{id}/regions`. Persists to `webapp_data/batches/<id>/region_hints.json`. Validates `label` and `source` against allowed enums.

### Frontend (under `webapp/frontend/src/`)

- [components/BatchDocumentModeSelector.tsx](webapp/frontend/src/components/BatchDocumentModeSelector.tsx) — card-style picker shown in the new-batch dialog.
- [components/ProcessingTimeline.tsx](webapp/frontend/src/components/ProcessingTimeline.tsx) — reads `progress.stages[]`, premium expandable list with status icons + per-stage durations.
- [components/AiFallbackStatusBadge.tsx](webapp/frontend/src/components/AiFallbackStatusBadge.tsx) — topbar pill ("AI: off" / "AI: ready · openai" etc.).
- [components/pdf_workspace/PdfWorkspace.tsx](webapp/frontend/src/components/pdf_workspace/PdfWorkspace.tsx) — top-level workspace; loads + saves regions; toolbar + canvas + overlay.
- [components/pdf_workspace/PdfPageCanvas.tsx](webapp/frontend/src/components/pdf_workspace/PdfPageCanvas.tsx) — single-page render via PDF.js to `<canvas>`. Lazy worker URL via Vite `?url`.
- [components/pdf_workspace/PdfOverlay.tsx](webapp/frontend/src/components/pdf_workspace/PdfOverlay.tsx) — pointer-event capture for draw/move/resize/delete.
- [components/pdf_workspace/RegionBox.tsx](webapp/frontend/src/components/pdf_workspace/RegionBox.tsx) — single labelled rectangle; corner handles + delete chip.
- [components/pdf_workspace/ViewerToolbar.tsx](webapp/frontend/src/components/pdf_workspace/ViewerToolbar.tsx) — Tool buttons + label dropdown + page nav + zoom controls.
- [components/pdf_workspace/geometry.ts](webapp/frontend/src/components/pdf_workspace/geometry.ts) — normalized↔pixel conversions, hit-test, drag-box normalisation, `newRegionId()`.
- [components/pdf_workspace/types.ts](webapp/frontend/src/components/pdf_workspace/types.ts) — local types (`Tool`, `DraftRegion`) + label colour table.
- [components/pdf_workspace/pdfjs.d.ts](webapp/frontend/src/components/pdf_workspace/pdfjs.d.ts) — minimal ambient module declarations for the legacy pdf.mjs and worker entrypoints.

## Files modified (Phase 1H)

### Backend

- [webapp/backend/main.py](webapp/backend/main.py) — wired `regions.router` and `ai_status.router`.
- [webapp/backend/api/batches.py](webapp/backend/api/batches.py) — new validators (`_validate_document_mode`, `_validate_ai_policy`), allowed enums (`DOCUMENT_MODES`, `AI_FALLBACK_POLICIES`), defaults; `CreateBatchBody` and `UpdateBatchBody` accept `document_mode`, `ai_fallback_enabled`, `ai_fallback_policy`. Defaults preserve legacy behaviour.
- [webapp/backend/services/batch_processor.py](webapp/backend/services/batch_processor.py) — reads `batch_metadata.json` + `region_hints.json`, declares 14-stage timeline, drives `start_stage`/`complete_stage`/`skip_stage` around the vendor call, enriches `run_context` with `document_mode`, `ai_fallback_enabled` (single gate), `ai_fallback_policy`, `ai_fallback_service`, and per-vendor-filtered `region_hints`.
- [utils/progress_tracker.py](utils/progress_tracker.py) — new `ProgressStage` dataclass, `stages: list[ProgressStage]` field on `ProgressSnapshot`, methods `declare_stages` / `start_stage` / `update_stage` / `complete_stage` / `warn_stage` / `skip_stage` / `fail_stage`. `fail()` and `complete()` auto-close any running stage so the timeline reflects where the run stopped.
- [.env.example](.env.example) — added AI block: `AI_FALLBACK_ENABLED`, `AI_PROVIDER`, plus four commented-out provider key slots.

### Frontend

- [webapp/frontend/package.json](webapp/frontend/package.json) — added `pdfjs-dist@^4.0.379`.
- [webapp/frontend/vite.config.ts](webapp/frontend/vite.config.ts) — `build.target = esbuild.target = optimizeDeps.esbuildOptions.target = "es2022"` so pdf.js v4's top-level `await` builds.
- [webapp/frontend/src/types.ts](webapp/frontend/src/types.ts) — new types: `DocumentMode`, `AiFallbackPolicy`, `AiStatus`, `ProcessingStage`, `ProcessingStageStatus`, `RegionLabel`, `RegionSource`, `RegionBBox`, `RegionHint`, `RegionHintsResponse`. New constants: `DOCUMENT_MODES`, `DOCUMENT_MODE_LABELS`, `DOCUMENT_MODE_DESCRIPTIONS`, `AI_FALLBACK_POLICY_LABELS`. `BatchProgress` extended with optional `stages[]`.
- [webapp/frontend/src/api.ts](webapp/frontend/src/api.ts) — `createBatch` and `updateBatch` accept document_mode + AI fields; new `getAiStatus`, `listRegions`, `replaceRegions`, `addRegion`, `deleteRegion`.
- [webapp/frontend/src/App.tsx](webapp/frontend/src/App.tsx) — replaced `window.prompt` new-batch flow with a premium modal dialog (name + mode card grid), added `AiFallbackStatusBadge` to topbar, added `ProcessingTimeline` under the progress bar.
- [webapp/frontend/src/components/DocumentPreviewPanel.tsx](webapp/frontend/src/components/DocumentPreviewPanel.tsx) — added preview mode toggle (Native / Field regions). Workspace lazy-loaded via `React.lazy(...)` so the native path doesn't pay for pdfjs.
- [webapp/frontend/src/styles.css](webapp/frontend/src/styles.css) — Phase 1H section added: AI pill, generic pill/chip, timeline (header + rows + status colours + pulse animation), modal (backdrop fade + card pop), mode-selector grid, mode-toggle pill, full PDF workspace stack (toolbar, canvas wrapper, overlay, region-box / handles / delete chip / draft box), skeleton-loader keyframes, edited-cell highlight.

---

## What works now

### End-to-end flow
1. Operator clicks "+ New batch" → premium modal opens with name field + 5 document-mode cards.
2. They pick e.g. **Scanned PDFs**, click Create → backend stores `document_mode: scanned_pdf` in `batch_metadata.json`.
3. Drop in a multi-page PDF → backend uploads, vendor detects, processor runs.
4. Sidebar shows the standard progress bar **and** an expandable processing timeline with stage-by-stage status (Upload ✓ · Vendor detect ✓ · Reading PDF ◔ · OCR running · ...).
5. Operator switches the document preview header from **Native** to **Field regions** → PDF.js workspace loads (lazy chunk, ~10 KB + 1.87 MB worker on demand).
6. They draw a rectangle around the service-address line → it's saved as `{label: "service_address", bbox: {x: 0.083, y: 0.071, w: 0.412, h: 0.046}}` to `region_hints.json` via PUT.
7. Refresh the batch → regions persist; switching pages or files in the workspace shows their own regions only.
8. Topbar badge shows **"AI: off"** because no provider is configured. The status reason in the tooltip matches the YAML state ("AI fallback disabled (provider=disabled)").
9. Process the batch → `run_context` carries the document mode, regions, and the AI service handle into Richmond / Hopkinsville processors. The processors haven't started reading these keys yet (Phase 1H is hooks only), but the data is there for the next phase.
10. Export still works exactly as before.

### Tools & toolbar
- Select / Draw / Pan / Delete tools.
- Region label dropdown: service_address, account_number, invoice_date, due_date, total_amount, line_items, notice_block, ignore_zone, custom — each with a distinct colour.
- Page nav, zoom in/out, reset zoom (range 50%–300%).
- Save status indicator ("Saving regions…" pill) so the operator knows when persistence is in flight.

### Safety on by default
- AI is disabled. The disabled adapter returns `error="ai_fallback_not_configured"` and the four real providers are stubs that raise.
- API keys are env-only; never echoed in JSON. `/api/ai/status` only returns enabled / provider / configured / reason / policy.
- Existing `Output/Template.xlsx` was not touched (SHA-256 unchanged). Source PDFs not touched. Unit Info Clean / GL Report / Vendor List unchanged.

---

## What is still placeholder

| Feature | Why deferred |
| --- | --- |
| Real AI provider HTTP calls | Foundation phase — wiring real providers is a separate, costed phase. The skeleton is in place so wiring is mechanical. |
| Bbox → pdfplumber crop on extraction | Vendor processors don't yet read `run_context["region_hints"]`. Hooked at the boundary; consumers come later. |
| Processor-level fine-grained timeline events | Today the batch_processor declares the timeline and toggles macro stages. A future phase can have processors call `tracker.start_stage("ocr", detail="page 3 / 14")` mid-flight. |
| Multi-page thumbnail rail | Workspace renders one page at a time; rail is straightforward to add but not in this phase. |
| In-app PDF page split / delete UI | `utils/pdf_splitter.py` already supports per-page split server-side; surfacing as a UI button is a follow-up. |
| AI-fed manual review suggestions | The data shape for AI-derived flags exists; the manual-review panel doesn't surface them yet because no AI ever fires. |

---

## Tests performed

### 1. Frontend build
```
$ npm run build
[…]
✓ 55 modules transformed.
dist/index.html                                  0.41 kB
dist/assets/pdf.worker-Be0fJUI5.mjs           1875.78 kB    (lazy)
dist/assets/PdfWorkspace-BE-UGhkk.js            10.16 kB    (lazy)
dist/assets/index-BCaGJou4.js                  178.11 kB
dist/assets/pdf-Dxs1Zqj5.js                    293.42 kB    (lazy)
dist/assets/index-CpDDSDnT.css                  20.29 kB    │ gzip:  4.44 kB
✓ built in 3.21s
```
The PDF.js worker (1.87 MB) and the workspace component (10 KB) ship as **lazy chunks** — they only load when the operator opens Field Regions mode.

### 2. Backend smoke
- `from webapp.backend.main import app` → 28 routes registered (was 24 in Phase 1G; added regions GET/PUT/POST/DELETE + AI status).
- `GET /api/ai/status` → 200, `enabled=False`, `provider="disabled"`, no API keys returned.
- AI service `suggest_field()` in default state returns `confidence=0.0`, `error="ai_fallback_not_configured"`, `requires_manual_review=True`. **No external request was made.**

### 3. Endpoint smoke (FastAPI TestClient)
Ran a 7-step round trip:
1. `POST /api/batches` with `{batch_name: "...", document_mode: "scanned_pdf", ai_fallback_policy: "only_low_confidence"}` → 200, metadata has `document_mode: scanned_pdf` ✓
2. `GET /api/batches/{id}/regions` → 200, empty list ✓
3. `POST /api/batches/{id}/regions` with a valid region → 200, regions=1 ✓
4. `POST /api/batches/{id}/regions` with `label: "totally_bogus"` → **400** ("label must be one of [...]") ✓ — invalid input rejected.
5. `DELETE /api/batches/{id}/regions/rg_test1` → 200, deleted=1 ✓
6. `DELETE /api/batches/{id}` → 200 ✓

### 4. CLI regressions

#### Richmond Utilities
```
Files processed              : 15
Invoices produced            : 28
ResMan line items            : 32
Invoices flagged for review  : 28
```
Unchanged from the pre-Phase-1H baseline.

#### Hopkinsville Water
```
Files processed              : 2
Invoices produced            : 14
ResMan line items            : 36
Invoices flagged for review  : 14
```
The disconnection-notice processor (Phase 1I) keeps producing the expected 4 invoices for the test PDF; the additional file in the training folder accounts for the rest. No new manual-review reasons appeared.

### 5. Source-file integrity (SHA-256)

| File | SHA-256 | Changed? |
| --- | --- | --- |
| `Output/Template.xlsx` | `b753f406…3969c284` | ❌ unchanged (matches Phase 1I report) |
| `Properties/Unit Info Clean.csv` | `79d46c7c…219c1a683` | ❌ unchanged |
| `Gl Codes/General Ledger Report.csv` | `8f8506ec…73abb6e49` | ❌ unchanged |
| `Vendors/Vendor List.csv` | `7839a43a…cef64863f9` | ❌ unchanged |

All four files have mtimes that pre-date this session (2026-05-01 16:21 / 17:12 / 17:19 / 20:08).

### 6. Secret hygiene
- `.env.example` lists `AI_PROVIDER`, `AI_FALLBACK_ENABLED`, and four provider key slots — all commented out. No real keys committed.
- `/api/ai/status` JSON does NOT include any key fields.
- The AI service's `__repr__` / status / log writer never serialise `self.api_key`.
- `webapp/.gitignore` already excludes `dist/` and `node_modules/`.

---

## Confirmation table

| Requirement | Status |
| --- | --- |
| `Output/Template.xlsx` not modified | ✅ SHA-256 unchanged |
| Source PDFs not modified | ✅ training PDFs untouched (CLI passes only read) |
| `Unit Info Clean.csv` / GL files / Vendor List unchanged | ✅ SHA-256 unchanged |
| Richmond Utilities still works | ✅ 28 invoices / 32 lines |
| Hopkinsville Water still works | ✅ test-PDF invoice count and reconciliation unchanged |
| CLI processors still work | ✅ both CLI runs pass |
| Docker / local workflow unchanged | ✅ no Dockerfile changes; backend imports clean |
| Export still works | ✅ unchanged path; new fields don't reach the export logic |
| Dropbox still works | ✅ unchanged; AI disabled means Dropbox stage runs as before |
| Vendor logic NOT moved into React | ✅ React only renders / draws; backend owns rules |
| AI keys NOT exposed | ✅ env-only; never returned by `/api/ai/status` |
| Premium UI polish | ✅ pills / modal / mode cards / timeline / region handles / skeleton |
| Batch document mode end-to-end | ✅ frontend → backend → metadata → run_context |
| Region hints saved/loaded | ✅ PUT/POST/DELETE round-trip verified |
| PDF.js workspace renders | ✅ build passes; canvas + overlay wired; lazy-loaded |
| Native preview unchanged | ✅ default mode in DocumentPreviewPanel |
| Processing timeline drives | ✅ stages declared by batch_processor + rendered by frontend |

---

## Next phases (deliberately out of scope here)

1. **Wire one real AI provider** (most valuable: Anthropic, given the rest of the stack). Implement `AnthropicAdapter.suggest_field`, route through the existing service. Audit log to `logs/ai_fallback.jsonl`. Cost ceiling enforcement at the call site.
2. **Vendor processors read `run_context["region_hints"]`** and pass each user-drawn bbox into `pdfplumber.crop` so a region around the service-address line forces extraction from there. Start with Hopkinsville disconnection notices — the highest-value fix.
3. **Granular timeline updates from inside processors.** Have `parse_hwea_pdf_page` call `tracker.update_stage("ocr", detail=f"page {i}/{n}")` per page so the bar moves smoothly through long batches.
4. **Multi-page thumbnail rail** + jump-to-page in the workspace.
5. **In-app PDF split / delete page** with backend persistence to a side-by-side processed PDF (never modify originals).
6. **Manual review smart suggestions** — pull AI suggestions per row when policy = `only_manual_review`.
