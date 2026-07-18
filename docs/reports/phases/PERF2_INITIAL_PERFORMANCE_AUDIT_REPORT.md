# Phase PERF-2 Initial Performance Audit Report

Run date: 2026-05-21
Workspace: `<repository-root>`
Frontend: `http://localhost:5174`
Backend: `http://localhost:8001`

This report was created before PERF-2 code optimizations. It combines code inspection, existing PERF-1 findings, and a fresh browser/API baseline against a populated Alabama Power batch.

## Baseline Measurements

Browser flow:

- Open app: 468 ms
- Switch to `Alabama May 26 TGAP`: 2729 ms
- Rendered grid size: 38 rows, 608 table cells
- Edit commit interaction: 780 ms
- Search/filter interaction: 425 ms
- Requests during open + switch flow: 27

Raw evidence:

- `docs/reports/phases/screenshots/phase_perf2/baseline_frontend_flow.json`
- `docs/reports/phases/screenshots/phase_perf2/baseline_bulk_loaded.png`

Most repeated requests during switch:

- `GET /api/batches`: 2
- `GET /api/ai/status`: 2
- `GET /api/health`: 2
- `GET /api/processing/queue`: 5
- `GET /api/batches/{id}/regions`: 2
- `GET /api/batches/{id}/documents/{file}/trace`: 3
- `GET /api/batches/{id}/files/{file}/content`: 6 across nearby PDFs

## Findings

| Area | Severity | Bottleneck / Root Cause | Files involved | Recommended fix | Expected impact | Safe in PERF-2 |
|---|---:|---|---|---|---|---|
| Batch switching | High | Switching a batch loads metadata, preview, manual review, revisions, file preview, regions, traces, and multiple PDF contents. Some calls are sequential or repeated. | `webapp/frontend/src/App.tsx`, `DocumentPreviewPanel.tsx`, `PdfWorkspace.tsx`, `api.ts` | Add AbortController/stale guards where missing, parallelize independent calls, cache stable batch/file metadata, reduce eager viewer fetches. | Faster switches, fewer blank states, fewer stale overwrites. | Yes |
| Template grid render | High | `ResManTemplatePreview` renders every row/cell with inline handlers and computed style objects. `activeDocumentRef`, selection, and edits can force broad rerenders. | `ResManTemplatePreview.tsx`, `TemplateWorkspace.tsx` | Memoize row/cell rendering, keep active edit as the only controlled input, debounce filter/search, avoid passing unstable props. | Faster edit/search and less UI lag. | Yes |
| Search/filter | Medium | Search recomputes visibility against all rows on every keystroke. This is acceptable at 38 rows but grows poorly at 500+ rows. | `TemplateWorkspace.tsx` | Debounce search/filter derivation and memoize grouped row data. | Smooth typing on large utility batches. | Yes |
| Single Invoice Mode | Medium | Large component body performs invoice-level calculations and fires GL candidate requests per invoice group. Effects depend on broad `group` object identity. | `TemplateWorkspace.tsx` | Memoize group keys, reduce effect dependencies, avoid repeated candidate fetches when descriptions unchanged. | Less lag switching invoices. | Yes, carefully |
| Document viewer | High | Viewer eagerly builds combined PDF document list, can fetch neighboring content/traces, and rerenders when unrelated parent state changes. | `DocumentPreviewPanel.tsx`, `PdfWorkspace.tsx`, `PdfPageCanvas.tsx` | Memoize viewer components, cache preview payloads, abort stale preview requests, preserve last rendered page while next page renders. | Less flicker and faster document navigation. | Yes |
| PDF page rendering | Medium | Canvas rendering has cancellation logic, but cache is per component lifecycle and pages still mount eagerly in long documents. | `PdfPageCanvas.tsx`, `PdfWorkspace.tsx` | Keep stale render cancellation, add render timing marks, avoid remounting workspace key unless document set really changes. | Fewer blank flashes. | Yes |
| Trace overlays | Medium | Trace fetches repeated during batch switch and viewer navigation; overlay changes can rerender viewer. | `PdfWorkspace.tsx`, `DocumentPreviewPanel.tsx`, `api.ts` | Cache traces by `batchId:filename`, abort stale trace fetches, memoize trace maps. | Lower request count and less viewer churn. | Yes |
| Batch dropdown | Medium | Dropdown loads a full batch list and detailed row structure; list can be large. Existing virtual window helps but search is immediate. | `BatchSelectorDropdown.tsx`, `BatchExplorer.tsx` | Debounce search, lazy-load expanded file details only, memoize rows. | Faster open/scroll. | Yes |
| Progress polling | Medium | Queue/progress/health/status polling continues during UI interactions and contributes to request noise. | `App.tsx`, `api.ts` | Pause or slow noncritical polling while idle, use visibility-aware polling, avoid duplicate initial refresh. | Less background churn. | Yes |
| Backend timing | Medium | PERF-1 records broad vendor-level steps but lacks jsonl event stream and finer steps for preview/manual review/revision write. | `perf_timer.py`, `processing.py`, `preview.py`, `batch_processor.py` | Write `performance.jsonl`, add specific step names, include persisted endpoint response. | Better root-cause diagnosis. | Yes |
| OCR / screenshot path | Medium | OCR cache exists, but script does not measure full screenshot invoice processing path with preview validation. | `scripts/profile_screenshot_invoice.py`, `utils/ocr_cache.py` | Expand profile script to report OCR/vision/total and skip real AI unless isolated. | Honest screenshot target reporting. | Yes |
| AI / vision | High | Provider latency can dominate. PERF-1 flagged no persistent AI response cache. | `ai_provider.py`, `ai_vision.py`, `ai_invoice_processor.py` | Add timing records and cache only where prompt/model/file hash are stable; enforce one call per page and timeouts. | Lower repeat latency/cost. | Partially; avoid behavior changes unless cache opt-in and safe |
| Queue/cancel | Medium | Cancel is cooperative and mostly solid, but cancellation latency is not measured per batch. | `processing_queue.py`, `cancel_registry.py`, `processing.py`, `perf_timer.py` | Record cancel requested/settled timings, block revision write after cancel. | Easier verification of no sticky processing state. | Yes |
| CSS/layout | Medium | `styles.css` is over 20k lines with many overrides and broad selectors. This increases style recalculation and makes regressions likely. | `styles.css`, `brand-refresh.css` | Consolidate high-churn table/viewer selectors only; defer broad visual cleanup. | Less layout/style churn. | Partially |
| Project cleanup | Low | Repo has modified screenshots, temp PNGs, and old reports/scratch files. Some are intentional evidence. | root, `docs/reports`, `tmp_*.png` | Audit only; do not delete user/source assets. Move only safe generated evidence if needed. | Cleaner repo, lower confusion. | Yes |

## Fix Plan Accepted for PERF-2

Safe now:

- Add/upgrade timing instrumentation without sensitive content.
- Add dev-only frontend perf helpers.
- Memoize large React components and derived data.
- Debounce table and dropdown search.
- Abort stale preview/file requests.
- Add jsonl performance persistence and endpoint compatibility.
- Add profiling scripts or upgrade existing PERF-1 scripts.
- Add browser screenshots and QA evidence.

Deferred unless tests prove safe:

- Full table virtualization. It is risky because the grid uses sticky headers, horizontal scrolling, context menus, row selection, and trace-linked row highlighting. First pass should use memoized row rendering and debounced filtering.
- Persistent AI/vision cache. This can be behavior-sensitive because prompt/model/rules changes must invalidate correctly.
- Broad CSS splitting. The visual system has many recent user-driven changes; aggressive movement risks regressions.

## Initial Root Cause Summary

The app is not slow because of one single bug. The current drag comes from accumulated medium-cost work:

1. App-level state changes rerender large panels.
2. The template grid renders many cells with per-cell closures and style objects.
3. Batch switching fans out to many API calls and viewer fetches.
4. Document viewer work can be triggered by unrelated template/batch state.
5. Backend timings are too coarse to identify processor substeps.
6. Polling and health/status requests run alongside operator interactions.

PERF-2 should prioritize reducing render blast radius and request duplication before changing invoice extraction logic.
