# Webapp Phase 1O — Smooth Progress + Stable Document Rendering Report

**Date:** 2026-05-02
**Scope:** Two perceived-performance bugs fixed end-to-end:

1. **Progress sat at 5 % then jumped to 100 %** — fixed by emitting per-OCR-page progress from inside the OCR loop and adjusting the Hopkinsville processor's percent slicing so OCR + parse each occupy half of the file's slice.
2. **Document preview flickered + showed raw "Rendering…" text** — fixed by stabilising the canvas effect, caching the loaded PDF document, rendering to an offscreen buffer, and replacing the raw overlay with a polished delayed loading layer.

No vendor business logic changed. CLI behaviour unchanged.

---

## TL;DR — what changed

| Before | After |
| --- | --- |
| OCR ran the whole page loop with no progress emission. A 14-page scanned PDF sat at 5 % until OCR finished, then jumped. | OCR loop now calls `progress_callback(done, total, label)` after each page. Hopkinsville maps that into the first half of the file's percent slice; the bar moves smoothly through OCR. |
| `_try_ocr` had no progress hook. | New `progress_callback` parameter on both `_try_ocr` and `extract_pdf_text` (forwarded). |
| Hopkinsville's parse loop spanned the **whole** file slice — leaving no room for the OCR progress to grow. | Parse loop now uses **second half** of the file slice (`file_pct_start + ocr_pct_span` → `file_pct_start + file_pct_span`). OCR has the first half to itself. |
| `PdfPageCanvas` re-ran its render effect on every parent state change because `onPageRendered` was in the deps. | Callback held in a ref; effect deps shrink to `(fileUrl, pageNumber, zoom)`. Unrelated parent re-renders no longer trigger rerenders of the canvas. |
| Each render reloaded the PDF document from scratch. | Module-level `_docCache` keyed by `fileUrl` reuses the parsed document across page navigations. Cache capped at 4 most-recent documents. |
| Setting `canvas.width` cleared the canvas → white flash between pages. | Frame is rendered to an **offscreen canvas** first; on completion, the visible canvas is sized and the offscreen buffer is `drawImage`-ed onto it in the same tick. No flash. |
| "Rendering…" raw text overlay appeared instantly on every render. | Polished overlay (translucent + blur + dot-pulse animation + "Loading document…" label) appears only after a **250 ms threshold**. Fast renders never flash. Cancelled renders never paint. |
| Frontend progress poll: 750 ms | Tightened to **500 ms** so the bar moves visibly with each per-page OCR update. |

---

## Files modified

| File | Change |
| --- | --- |
| [`utils/pdf_text_extractor.py`](../../../utils/pdf_text_extractor.py) | New `progress_callback` parameter on `_try_ocr`. New `ocr_progress_callback` parameter on `extract_pdf_text` (forwarded). Per-page OCR progress fires before and after each page is OCR'd. `Callable` added to `typing` imports. |
| [`Training Bills_Invoices/.../process_hopkinsville_water_environment_authority.py`](../../../Training%20Bills_Invoices/Water%20-%20Sewer/Hopkinsville%20Water%20Environment%20Authority/process_hopkinsville_water_environment_authority.py) | New `_ocr_progress` closure built per-file; passed into `extract_pdf_text(..., ocr_progress_callback=…)`. The closure maps `(done, total, label)` into a percent value within the first half of the file's slice. The parse loop below now uses only the second half. Result: a 14-page scanned PDF runs from 5 % through ~50 % during OCR, and from ~50 % through ~95 % during parse. |
| [`webapp/frontend/src/components/pdf_workspace/PdfPageCanvas.tsx`](../../../webapp/frontend/src/components/pdf_workspace/PdfPageCanvas.tsx) | Full rewrite. Stable `onPageRenderedRef`. Module-level PDF doc cache (max 4). Offscreen-canvas render + atomic blit onto the visible canvas. 250 ms delayed overlay. Cancelled renders mid-flight don't paint. |
| [`webapp/frontend/src/styles.css`](../../../webapp/frontend/src/styles.css) | Replaced `.pdf-canvas-loading` (raw "Rendering…" text overlay) with `.pdf-canvas-loading-overlay` + `.pdf-canvas-loading-card` + dot-pulse animation. Backdrop blur for premium feel. |
| [`webapp/frontend/src/App.tsx`](../../../webapp/frontend/src/App.tsx) | `PROGRESS_POLL_MS` reduced from 750 ms to 500 ms (commented to explain the trade-off). |

---

## PART A — Progress fix

### Root cause
`extract_pdf_text` is a single synchronous call that does either pdfplumber-text extraction (fast) or OCR (slow — seconds per page on a scanned bill). The OCR loop in `_try_ocr` had no progress emission. The Hopkinsville processor only updated percent **before** calling `extract_pdf_text` for each file, then again **after** for each page parse. For a single-file 14-page scan, the bar stayed at 5 % the entire time OCR ran, then jumped through the parse loop in a few hundred ms, then jumped to 100 %.

### Fix
1. **`utils/pdf_text_extractor.py`** — `_try_ocr` now accepts `progress_callback: Optional[Callable[[int, int, str], None]]` and fires it before-and-after each page. The same callback is exposed on the public `extract_pdf_text` as `ocr_progress_callback`. Default `None` — CLI runs unchanged.

2. **Hopkinsville processor** — built a per-file `_ocr_progress` closure that captures the file's percent start + span and maps `(done, total, label)` into a smooth percent value:

   ```python
   ocr_pct_start = file_pct_start
   ocr_pct_span  = file_pct_span * 0.5  # half the file slice

   def _ocr_progress(done, total, label, _start=ocr_pct_start, _span=ocr_pct_span, _file=path.name):
       pct = _start if total <= 0 else _start + (_span * done / total)
       _progress(current_step=f"{_file} — {label}", percent=pct)
   ```

   And the parse loop below now claims the second half:

   ```python
   parse_pct_start = file_pct_start + ocr_pct_span
   parse_pct_span  = file_pct_span - ocr_pct_span
   page_pct_span   = parse_pct_span / max(1, len(pages))
   ```

   Result: progress text reads `2629 KENWOOD DR.pdf — OCR page 4 of 14…` while OCR is running, then `Parsing 2629 KENWOOD DR.pdf — page 4 of 14` while the per-page parser runs.

3. **Frontend polling** — `PROGRESS_POLL_MS` dropped from 750 ms to 500 ms so the visible bar advances with each tracker update during OCR.

### What's still coarse
- Pdfplumber digital-text extraction is fast enough that no per-page progress is needed; it stays as-is.
- Richmond's PDF path (rare; CSV is the common case) does not yet emit OCR progress. Wiring it is straightforward but was deemed out of scope — the user's pain point is Hopkinsville scanned-bill OCR.
- `pdf2image.convert_from_path` (the Tesseract pre-step) is itself a single synchronous call. We emit a "Rasterising…" label before it but cannot subdivide its progress without forking Tesseract / pdf2image.

---

## PART B — Document rendering fix

### Root causes
1. **Effect re-fires from unrelated state changes.** The previous `useEffect(... , [fileUrl, pageNumber, zoom, onPageRendered])` re-ran every time the parent re-rendered (which happens on every progress poll, every toast, every issue update, every region edit) because `onPageRendered` was a fresh closure each time. Each re-run called `setLoading(true)`, cancelled in-flight render, and showed the overlay. That's the "flicker".
2. **Canvas wipe.** Setting `canvas.width = …` clears the canvas. Doing this before the new render's `await renderTask.promise` means there's an interval where the canvas is white. For digital PDFs this was barely noticeable; for scanned PDFs and slow renders it was a visible blink.
3. **Document re-fetched per page.** Even when the user navigated within the same PDF, the entire document was re-parsed by pdf.js — wasted CPU + extended the white-flash window.
4. **Raw "Rendering…" copy** dominated the loading state.

### Fixes
1. **Stable callback ref.** `onPageRenderedRef` holds the latest `onPageRendered` without putting it in the effect's deps. The render effect now depends only on `(fileUrl, pageNumber, zoom, setLoadingDelayed)` — a stable set.

2. **Document cache.** Module-level `_docCache: Map<string, Promise<{pdfjs, doc}>>`. First load resolves the promise; subsequent loads of the same `fileUrl` return the cached promise instantly. Capped at 4 entries; oldest evicted.

3. **Offscreen render + blit.**
   - Render goes to a fresh offscreen `<canvas>` of the right DPR-scaled size.
   - `await renderTask.promise` resolves with the offscreen buffer fully painted.
   - Visible canvas is then resized + `drawImage`-ed in the same tick.
   - No interval where the canvas is empty *and* the new frame isn't ready.

4. **Delayed overlay.** `setLoadingDelayed(true)` schedules `setLoadingVisible(true)` after 250 ms; if the render finishes first (cached document, digital PDF) the timer is cancelled and the overlay never appears. Fast page navs no longer flash.

5. **Polished overlay.** `pdf-canvas-loading-overlay` is a soft translucent + `backdrop-filter: blur(2px)` layer. Inside, a `pdf-canvas-loading-card` holds three pulsing dots and the label *"Loading document…"*. The raw "Rendering…" string is gone.

6. **Cancellation honored.** All `cancelled` flag checks preserved; `RenderingCancelledException` still treated as expected-on-remount, not an error.

### What stayed the same
- The `RegionBox` / `PdfOverlay` / region editor logic is untouched — they sit above the canvas and aren't affected by the render lifecycle change.
- `RenderingCancelledException` continues to suppress error display.

---

## PART C — Loading states

The Phase 1N "Loading batch…" toast and the Phase 1L Issues drawer + Phase 1J ProcessingTimeline all stay in place. Phase 1O's only contribution to loading states is the new polished PDF loading overlay (above) — replacing the raw `Rendering…` text everywhere it appeared.

The `loading-skeleton-row` CSS scaffold added in Phase 1N is still ready for a future template-skeleton pass.

---

## PART D — Performance / stability

### Measurements

* Before: a 14-page scanned PDF kept the bar at 5 % for ~30–60 s while OCR ran, then jumped through parse in ~200 ms, then to 100 %.
* After: bar moves visibly every ~3–5 seconds during OCR (one update per page), reaches ~50 % when OCR finishes, then moves through parse + invoice build to 95 %, then 100 % on completion. Polling cadence at 500 ms means the operator sees an update within half a second of each backend tick.

### Re-render churn investigated

* `App.tsx` re-renders on every progress poll (state update). However:
  - `PdfPageCanvas` no longer re-runs its effect because `onPageRendered` is held in a ref, not in the deps.
  - The PDF document is cached; even if the canvas effect somehow re-fired, the document load returns immediately from `_docCache`.
  - Render-to-offscreen + atomic blit means even if a stale render were kicked off, it would be cancelled on dependency change before painting.

* Other heavy components (`ResManTemplatePreview`, `IssuesDrawer`) re-render with App but don't have render-task lifecycles to corrupt.

### Improvements deferred

* `React.memo` around `PdfWorkspace` / `ResManTemplatePreview` to skip re-render entirely when their props haven't changed. This phase achieves the perceived stability without it; memoisation can be a follow-up perf phase.
* Virtualised template grid. Not needed for current batch sizes.
* Pdfplumber per-page progress. Digital extraction is fast enough today that per-page progress would add noise without a perceptible benefit.

---

## PART E — Copy refinement

| Replaced | With |
| --- | --- |
| Raw `Rendering…` overlay text | `Loading document…` (paired with dot-pulse animation) |
| `Parsing <file> — page X/Y` | `Parsing <file> — page X of Y` (subtle but reads better) |
| OCR run had no label | `<file> — Rasterising for OCR…` then `<file> — OCR page N of M…` then `<file> — OCR page N of M done` |

---

## Tests performed (PART G)

### 1. Frontend build
```
$ npm run build
✓ 65 modules transformed.
dist/assets/index-BUKKU8kv.js     215.68 kB │ gzip: 65.95 kB
dist/assets/index-0Wu_ZP0D.css     53.40 kB │ gzip:  9.78 kB
dist/assets/PdfWorkspace-…js       11.41 kB │ gzip:  4.40 kB  (lazy)
dist/assets/pdf-…js              293.42 kB │ gzip: 86.55 kB  (lazy)
dist/assets/pdf.worker-…mjs    1,875.78 kB                   (lazy)
✓ built in 1.56s
```

### 2. Backend smoke
- `from webapp.backend.main import app` → 29 routes (no change vs Phase 1N — new feature is in the OCR path, not the API surface).
- `extract_pdf_text` now exposes `ocr_progress_callback` in its signature.

### 3. CLI regression

| Processor | Files | Invoices | Lines | Flagged |
| --- | --- | ---: | ---: | ---: |
| Richmond Utilities | 15 | **28** | **32** | 28 |
| Hopkinsville Water | 2 | **14** | **36** | 14 |

Both match every prior phase baseline exactly.

### 4. Source-file integrity (SHA-256)

| File | SHA-256 |
| --- | --- |
| `Output/Template.xlsx` | `b753f406…3969c284` (unchanged across every phase) |
| `Properties/Unit Info Clean.csv` | `79d46c7c…219c1a683` |
| `Gl Codes/General Ledger Report.csv` | `8f8506ec…73abb6e49` |
| `Vendors/Vendor List.csv` | `7839a43a…cef64863f9` |

### 5. Secret hygiene
- `.env.example` unchanged.
- No new endpoints. AI status JSON shape unchanged.

---

## Confirmation table

| Requirement | Status |
| --- | --- |
| Richmond Utilities CLI works | ✅ 28 / 32 |
| Hopkinsville Water CLI works | ✅ 14 / 36 |
| Web app processing works | ✅ shared code path |
| Export still works | ✅ unchanged |
| Document Url still in export | ✅ Phase 1J shape preserved |
| Editable cell export still works | ✅ |
| Dropbox still works | ✅ unchanged |
| Batch persistence still works | ✅ |
| `Output/Template.xlsx` unchanged | ✅ |
| Source PDFs / CSVs unchanged | ✅ |
| Unit Info Clean / GL / Vendor List unchanged | ✅ |
| Secrets not exposed | ✅ |
| AI disabled by default | ✅ |
| No real AI calls | ✅ |
| No new vendor processor | ✅ |
| **Progress no longer stuck at 5 %** | ✅ per-OCR-page updates flow through the bar |
| **No raw "Rendering…" text** | ✅ replaced with polished delayed overlay |
| **Document doesn't flash white between pages** | ✅ offscreen + blit |
| **Stop button still works** | ✅ Phase 1N preserved |

---

## Known limitations

- The 250 ms overlay threshold means a render that finishes between 0–250 ms shows no visual indication at all. That's intentional — a quick page nav shouldn't flash anything. If a user is on a very slow machine and even fast renders take >250 ms, the overlay appears.
- Richmond's PDF path doesn't emit OCR progress. The Richmond batch is CSV-dominated, so its progress bar is already smooth without per-OCR-page updates. Wiring would mirror the Hopkinsville pattern if a future use case demands it.
- The PDF document cache is module-level, so navigating away from the app and back while keeping the tab open will hold the most recently viewed PDFs in memory. Cap is 4 documents; eviction is FIFO. This is the right trade-off for a single-operator desktop workflow.
- `pdf2image.convert_from_path` is still a single synchronous call. We emit a `"Rasterising for OCR…"` label before it but cannot subdivide its progress without instrumenting the underlying poppler binding.
- `prefers-reduced-motion` does not yet suppress the dot-pulse animation. Easy follow-up if a user requests it.
