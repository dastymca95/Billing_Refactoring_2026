# Webapp Premium / AI / PDF Workspace — Architecture Plan

**Date:** 2026-05-02
**Phase scope:** 1H — *foundation* only. CLI, Richmond, Hopkinsville, Dropbox, export must keep working unchanged.

---

## TL;DR

The web console becomes a property-manager-friendly invoicing workspace with five new capabilities:

1. **Batch document mode** — operator declares whether the batch is digital PDFs, scanned PDFs, mixed, CSV/Excel, or auto-detect; backend routes accordingly.
2. **AI fallback layer** — disabled by default, provider-agnostic skeleton; only fires when rules + OCR can't resolve a field. Never overrides Unit Info Clean / GL evidence / bill-total reconciliation.
3. **PDF workspace** — PDF.js canvas + HTML overlay for drawing labelled rectangles (service address, account, total, etc.). Coordinates stored normalized 0–1 so they survive zoom/resize. Native `<iframe>` viewer remains for fast browse.
4. **Processing timeline** — every stage (vendor detect, OCR, YAML rules, GL match, AI fallback, Dropbox upload, template build) shown as a labelled step with status, duration, warnings count.
5. **Premium UI polish** — compact sidebar, skeleton loaders, smooth panel transitions, sticky table headers, better edited-cell highlights, row status badges.

The existing Python processors stay authoritative. The frontend gets richer, the backend gets a few new hooks, and AI is a strictly-bounded fallback — not the engine.

---

## What we're borrowing from Rivera (conceptually)

| Rivera idea | Why it fits here |
| --- | --- |
| Native `<iframe>` viewer for fast preview | We already use this; keep it. |
| PDF.js canvas + absolute HTML overlay | We need to draw region rectangles without touching the PDF. |
| **Normalized bbox** `(x, y, w, h) ∈ [0,1]` | Survives zoom, screen size, and render DPR. Critical for our cross-vendor reuse. |
| Don't re-render PDF on every mouse move | Move the overlay's CSS/state instead. Keeps interaction smooth. |
| Commit changes on `mouseup` | Fewer re-renders, easier undo. |
| Future extraction: bbox → pdfplumber crop | Backend already uses pdfplumber; a future phase converts a normalized bbox to PDF-coordinate crops. |

We are *not* copying Rivera's data model verbatim — our regions need vendor-aware semantics (service address, account, due date, etc.). We *are* copying the rendering architecture (canvas + overlay + normalized coords).

---

## Native preview vs. PDF workspace

Two viewing modes; the operator picks. Default is native (fast).

| Mode | Component | Tech | Use cases |
| --- | --- | --- | --- |
| Native preview | `DocumentPreviewPanel` | browser `<iframe>` | scrolling, quick visual check, default |
| Field Region Mode | `PdfWorkspace` | `pdfjs-dist` canvas + overlay | drawing/labelling extraction zones, troubleshooting OCR misses |

A toggle in the document-preview header switches between them. PDF.js is loaded **lazy** (dynamic import) so the native preview path doesn't pay for it.

---

## Region model

Stored in `webapp_data/batches/<batch_id>/region_hints.json`:

```jsonc
{
  "schema_version": 1,
  "regions": [
    {
      "id": "rg_a8c2…",
      "file_id": "HWEA - Aspen - 3-16-26.pdf",   // filename inside batch input/
      "page_number": 1,                          // 1-indexed
      "bbox": { "x": 0.083, "y": 0.071, "w": 0.412, "h": 0.046 },
      "label": "service_address",
      "color": "#0969da",
      "notes": "",
      "source": "user",                          // user | ai | rules
      "confidence": 1.0,
      "created_at": "2026-05-02T13:45:00",
      "updated_at": "2026-05-02T13:45:00"
    }
  ]
}
```

**Region labels**: `service_address`, `account_number`, `invoice_date`, `due_date`, `total_amount`, `line_items`, `notice_block`, `ignore_zone`, `custom`.

**Why normalized bbox**: a region drawn at 1.5× zoom on a 1920px screen still maps correctly when the same PDF is rendered at 1.0× on a 1280px screen. Backend converts back to PDF coordinates with `pdf_w * x` etc.

---

## AI fallback role

Strict policy (codified in `config/ai_fallback_rules.yaml`):

- **Rules-first.** YAML regex bank + Unit Info Clean + GL evidence run first. AI fires only when a field is missing OR confidence is below threshold OR the manual_review queue would have been longer otherwise.
- **Never override validated data.** No AI write to Location unless Unit Info Clean has the (property, unit) pair. No AI write to Property Abbreviation unless validated. No AI silent amount correction.
- **Audit every AI suggestion.** Output schema includes `field_name`, `suggested_value`, `confidence`, `source_text_excerpt`, `provider_used`, `cost_estimate`, `requires_manual_review`. Every AI-derived field gets `manual_review_reason: ai_filled_field` so an operator must confirm.
- **Disabled by default.** `AI_FALLBACK_ENABLED=false` and `AI_PROVIDER=disabled`. App must work — and the UI must clearly say "AI fallback disabled or not configured" — when no key is set.
- **Provider-agnostic.** Adapter pattern: `OpenAIAdapter`, `AnthropicAdapter`, `GoogleGeminiAdapter`, `DeepseekAdapter`, all behind a common `AIProvider` ABC. Selection via `AI_PROVIDER` env var.
- **Cost guard.** Per-batch USD ceiling (`max_cost_per_batch_usd`) enforced before each call.

The skeleton ships with one functional adapter (`DisabledAdapter` returning *"AI fallback not configured"*) plus typed stubs for the four providers. Wiring real provider calls is a follow-up phase — Phase 1H does NOT make any external API calls.

---

## Batch document modes

Stored in `batch_metadata.json`:

```jsonc
{
  "batch_id": "batch_20260502_…",
  "batch_name": "May 2026 Hopkinsville",
  "document_mode": "auto_detect",   // digital_pdf | scanned_pdf | mixed_pdf | csv_excel | auto_detect
  "ai_fallback_enabled": true,
  "ai_fallback_policy": "only_low_confidence",   // never | only_low_confidence | only_manual_review | always_assist
  "created_at": "…",
  "updated_at": "…"
}
```

Mode influences processing:

- `digital_pdf`: try `pdfplumber.extract_text()` first; only fall back to OCR if that returns 0 chars on a page.
- `scanned_pdf`: skip the digital text attempt, go straight to OCR. Saves a second per page on known-scanned PDFs.
- `mixed_pdf` / `auto_detect`: today's behaviour — try digital, fall back to OCR per page.
- `csv_excel`: skip PDF/OCR pipeline entirely, route to existing CSV/XLSX handlers (Richmond reads `Bill Search.csv`).

CLI runs are unaffected — `document_mode=auto_detect` is the default and matches today's behaviour.

---

## Processing timeline

Backend `progress.json` gains a `stages` array (additive — old `current_step` / `percent` stay):

```jsonc
{
  "batch_id": "…",
  "status": "processing",
  "percent": 47.0,
  "current_step": "Running OCR on page 3 of 14",
  "stages": [
    {
      "key": "upload",
      "label": "Uploading files",
      "status": "completed",
      "started_at": "13:01:02",
      "completed_at": "13:01:04",
      "warnings_count": 0
    },
    {
      "key": "vendor_detect",
      "label": "Detecting vendor",
      "status": "completed",
      "started_at": "13:01:04",
      "completed_at": "13:01:05",
      "warnings_count": 0
    },
    {
      "key": "ocr",
      "label": "Running OCR",
      "status": "running",
      "started_at": "13:01:06",
      "detail": "page 3 / 14"
    },
    {
      "key": "yaml_rules",
      "label": "Applying vendor rules",
      "status": "pending"
    },
    // …
  ]
}
```

Stage statuses: `pending`, `running`, `completed`, `warning`, `failed`, `skipped`.

The frontend shows a compact bar (existing) plus an **expandable timeline** with one row per stage. Skeleton stage list comes from a small constants table; the processor adds entries as it goes. Old vendor processors that never call `tracker.start_stage()` simply have no `stages` key — frontend tolerates this.

---

## Backend changes (additive)

| File | Change |
| --- | --- |
| `webapp/backend/api/batches.py` | `CreateBatchBody` adds `document_mode`, `ai_fallback_enabled`, `ai_fallback_policy`. `UpdateBatchBody` accepts the same fields. `_summary_for_batch` includes them. |
| `webapp/backend/api/regions.py` *(new)* | `GET /api/batches/{id}/regions`, `PUT` (replace all), `POST` (append one), `DELETE /{region_id}`. Persists to `region_hints.json`. |
| `webapp/backend/api/ai_status.py` *(new)* | `GET /api/ai/status` returns `{enabled, provider, configured, reason}` so the UI knows whether to show "AI fallback disabled". |
| `webapp/backend/services/ai_fallback.py` *(new)* | Provider-agnostic skeleton; `is_enabled()`, `is_configured()`, `suggest_field()`, `suggest_unit_match()`. All return "not configured" today. |
| `webapp/backend/services/batch_processor.py` | Pass `document_mode`, `ai_fallback`, `region_hints` to processors via `run_context` — only when the processor accepts them (introspect, like today's `progress_callback`). |
| `utils/progress_tracker.py` | Add `start_stage(key, label)`, `complete_stage(key)`, `fail_stage(key, msg)`. |
| `config/ai_fallback_rules.yaml` *(new)* | Disabled-by-default rules, allowed/forbidden tasks, audit policy. |
| `.env.example` *(new or extended)* | `AI_PROVIDER`, `AI_FALLBACK_ENABLED`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `DEEPSEEK_API_KEY`. |

CLI default behaviour stays untouched: when none of these are set, the processor sees `document_mode=auto_detect` and `ai_fallback=None` and runs exactly as today.

---

## Frontend changes (additive)

| File | Change |
| --- | --- |
| `webapp/frontend/package.json` | Add `pdfjs-dist` + matching `@types`. |
| `src/types.ts` | New types: `DocumentMode`, `AiFallbackPolicy`, `AiStatus`, `RegionHint`, `ProcessingStage`. |
| `src/api.ts` | New methods: `getAiStatus`, `getRegions`, `replaceRegions`, `addRegion`, `deleteRegion`. Extend `createBatch` / `updateBatch` to pass document_mode + AI fields. |
| `src/components/BatchDocumentModeSelector.tsx` *(new)* | Card-style picker (digital / scanned / mixed / csv-excel / auto). Shows in the new-batch dialog. |
| `src/components/ProcessingTimeline.tsx` *(new)* | Stage list with status icons. Reads `progress.stages`. Falls back gracefully when missing. |
| `src/components/AiFallbackStatusBadge.tsx` *(new)* | Reads `/api/ai/status`. Shows "AI: off", "AI: ready", "AI: assisted N fields". |
| `src/components/pdf_workspace/PdfWorkspace.tsx` *(new)* | Top-level workspace component. |
| `src/components/pdf_workspace/PdfPageCanvas.tsx` *(new)* | Renders one page via PDF.js to a `<canvas>`. |
| `src/components/pdf_workspace/PdfOverlay.tsx` *(new)* | Absolute-positioned overlay; mouse capture for draw/move/resize. |
| `src/components/pdf_workspace/RegionBox.tsx` *(new)* | Single rectangle with handles. |
| `src/components/pdf_workspace/ViewerToolbar.tsx` *(new)* | Tool buttons (select / draw / pan / delete / zoom). |
| `src/components/pdf_workspace/geometry.ts` *(new)* | Normalized ↔ pixel conversions; clamp; hit-test. |
| `src/components/pdf_workspace/types.ts` *(new)* | `RegionHint`, `Tool`, `RegionLabel`. |
| `src/components/DocumentPreviewPanel.tsx` | Add a preview-mode toggle (native ↔ workspace). |
| `src/styles.css` | Premium polish: skeleton loaders, badges, smooth panel transitions, sticky table headers, AI status pill, timeline row styles. |

---

## Safety rules

- No source PDFs modified.
- `Output/Template.xlsx` not modified.
- No vendor logic moved into React.
- No secrets in code or repo. Every AI key reads from environment only; `.env` gitignored.
- AI calls fenced behind a single `is_enabled() and is_configured()` gate so a misconfiguration cannot accidentally call out to a paid API.
- Processor signatures are extended with kwargs that default to `None`; CLI invocations and Phase 1G/1I behaviour are byte-identical.
- Region hints are advisory inputs, not authoritative. A drawn box never overwrites Unit Info Clean / GL match output.

---

## Cost & secrets considerations

- **Per-batch ceiling.** `ai_fallback_rules.yaml` ships with `max_cost_per_batch_usd: 1.00` and the skeleton enforces it before every AI call (when wiring is added in a later phase).
- **Cropped requests by default.** When AI is wired up, prefer cropped region images (drawn rectangles) over whole pages — cheaper, less PII surface.
- **Redact tokens.** `redact_sensitive_tokens: true` strips obvious auth-like substrings before any payload leaves the box.
- **Manual review on AI-filled.** `require_manual_review_for_ai_filled_fields: true` means an AI suggestion never auto-flows to the export — operator must confirm.
- **Logged, not printed.** AI request/response bodies go to `webapp_data/batches/<id>/logs/ai_fallback.jsonl` (when enabled); never to the console.
- **Never persist API keys.** Keys only read at process start from `os.environ`; not echoed in `/api/ai/status` (only `enabled`, `provider`, `configured`).

---

## Phase 1H scope (this PR)

Build the foundation. Wire the hooks. Don't ship AI traffic.

✅ **Will land:**
- Batch metadata: `document_mode`, `ai_fallback_*` fields end-to-end (frontend → backend → metadata → run_context).
- `BatchDocumentModeSelector` component + new-batch dialog flow.
- Region hints storage + REST endpoints + frontend component for drawing/saving/deleting.
- PDF.js workspace renders one page, overlay supports draw/select/move/resize/delete.
- Processing timeline reads `stages[]` from progress JSON; empty stages list = legacy behaviour.
- AI fallback service skeleton (provider stubs, disabled adapter, status endpoint).
- Premium CSS pass on existing components.
- Documentation: this plan, the Phase 1H report, README updates, `.env.example` additions.

🟡 **Will not land in 1H (deferred):**
- Real AI provider HTTP calls.
- Bbox → pdfplumber crop conversion at extraction time.
- Multi-page workspace navigation (one page at a time is fine for foundation).
- Thumbnail rail.
- PDF page split/delete via UI (backend `pdf_splitter` already supports per-page split — surfacing is later).

🚫 **Will never land (out of scope by design):**
- AI overriding Unit Info Clean validation.
- AI auto-adjusting financial amounts.
- Vendor logic in React.
- Hardcoded API keys.
