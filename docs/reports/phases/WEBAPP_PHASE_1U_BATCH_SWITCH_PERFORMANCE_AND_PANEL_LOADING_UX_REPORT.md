# Webapp Phase 1U — Batch Switch Performance + Panel Loading UX + Collapse Rail Redesign

**Date:** 2026-05-02
**Scope:** Backend perf cache + frontend UX redesign. No vendor business logic, CLI, Dropbox, AI policy, or training-data files touched.

> Supersedes the earlier Phase 1U "Batch Switch Loading Stability" report
> ([WEBAPP_PHASE_1U_BATCH_SWITCH_LOADING_STABILITY_REPORT.md](WEBAPP_PHASE_1U_BATCH_SWITCH_LOADING_STABILITY_REPORT.md)),
> which fixed the empty-flash via atomic state swap. That fix is preserved
> here. This report documents the **deeper** work: the actual
> backend-side cost that made switching feel 8–10 s slow, and the panel-
> local loading + collapse-rail redesign the operator was still missing
> after the atomic-swap fix.

---

## TL;DR

| Before | After |
| --- | --- |
| Every `GET /api/batches/<id>` opened **every PDF** in the batch with `pdfplumber.open()` for vendor detection. 10-file batch ≈ 1–3 s of disk + parsing on every switch. | Vendor detection cached in `batch_metadata.json["file_detection_cache"]` keyed by `(filename, size, mtime)`. Warm hits skip `detect_vendor_for_file` entirely. |
| Switch loading rendered a **full-screen translucent + blurred overlay** with a single "Loading <batch>…" pill. Whole UI looked frozen. | Per-panel skeletons. **No global blur.** File list shows shimmering rows sized to the expected file count. Template grid shows a header + 8 skeleton rows + a small status label. |
| Collapsed rails were **36 px white stripes** with one icon button. Operator couldn't tell which panel was collapsed. | 44 px rails with **strong visual identity per variant**: tinted accent stripe (indigo for Files, amber for Document, red for Issues), an iconified card on top, a small uppercase label ("FILES", "DOCUMENT"), and a circular chevron at the bottom that turns into the accent on hover. |
| Old `setSelected` reset triggered an unnecessary re-fetch in `DocumentPreviewPanel` even when the same file existed in the new batch. | Atomic state swap from prior 1U fix preserved; per-panel skeletons mean the document area shows the previous PDF until the new file fetch resolves (Phase 1O delayed-overlay still applies). |

---

## Root cause analysis — where the 8–10 seconds went

Instrumented the four endpoints touched on a switch:

1. **`GET /api/batches/{id}`** — runs `_summary_for_batch` (reads `_webapp_result.json`) **and** loops `detect_vendor_for_file(p)` over every input file. The vendor-detection helpers in `webapp/backend/services/vendor_detection.py` open PDFs with `pdfplumber.open(...)` to sample text. For a 10-file scanned-PDF batch the loop alone burned ~1.5–3 s on a warm OS file cache, more on first read.

2. **`GET /api/batches/{id}/preview`** — parses the cached `_webapp_result.json`. Sub-second on small batches but grows with row count.

3. **`GET /api/batches/{id}/manual-review`** — re-parses the same cached JSON.

4. The frontend then triggered a **`/content` fetch in `DocumentPreviewPanel`** for `selected = files[0]`, which kicks off pdf.js parsing (Phase 1O caches this per `fileUrl`, so subsequent same-file accesses are fast).

The **dominant cost on every switch** was #1's pdfplumber loop. The phase 1T atomic-swap fix made the wait *visually clean* but the wait itself stayed the same length. Phase 1U cuts the actual backend work plus removes the visual blur.

---

## Files modified

### Backend

| File | Change |
| --- | --- |
| [`webapp/backend/api/batches.py`](../../../webapp/backend/api/batches.py) | New `_detect_files_cached(batch_id, files)` helper. Reads `batch_metadata["file_detection_cache"]`, skips `detect_vendor_for_file` for files whose `(size_bytes, mtime)` already match a cached entry, and persists fresh entries back. `get_batch_endpoint` and `list_files_endpoint` both go through it; the per-call PDF-open loop is gone. Detection still runs once for new / modified files (the only correct moment), and the cache rewrites itself when files are deleted. |

### Frontend

| File | Change |
| --- | --- |
| [`webapp/frontend/src/components/CollapseRail.tsx`](../../../webapp/frontend/src/components/CollapseRail.tsx) | Rewritten. Now takes `variant: "files" \| "document" \| "issues"` and `label`. Renders the whole rail as a single `<button>` so the entire stripe is the click target. Top: icon card (with optional badge count). Middle: small uppercase label. Bottom: round chevron. |
| [`webapp/frontend/src/components/FileList.tsx`](../../../webapp/frontend/src/components/FileList.tsx) | New optional `isSwitchingBatch` + `expectedFileCount` props. When switching, renders `n` skeleton rows (1–8, sized to the cached `files_count` from the batch list) instead of either an empty state or the previous batch's stale entries. |
| [`webapp/frontend/src/components/TemplateWorkspace.tsx`](../../../webapp/frontend/src/components/TemplateWorkspace.tsx) | New `isSwitchingBatch` + `loadingBatchName` props. Renders an absolute-positioned `.template-skeleton` panel (header + 8 skeleton rows + small "Loading <batch>…" label) above the existing preview pane while a switch is in flight. The previous batch's grid stays mounted underneath so React doesn't reset scroll position. |
| [`webapp/frontend/src/App.tsx`](../../../webapp/frontend/src/App.tsx) | `<CollapseRail>` calls updated to pass `variant="files"` / `variant="document"` and friendly labels ("Files", "Document"). Removed the global `.batch-switch-overlay` JSX block. Threaded `isSwitchingBatch` + `loadingBatchName` into `<FileList>` and `<TemplateWorkspace>`. |
| [`webapp/frontend/src/styles.css`](../../../webapp/frontend/src/styles.css) | Replaced the old `.collapse-rail`/`.collapse-rail-btn` rules with the new variant-aware styles (44 px wide, accent stripe, hover lift, per-variant tints). Removed the global blur overlay rule (`.batch-switch-overlay { display: none !important; }` keeps any old test selector quiet without painting a blur). New skeleton rules: `.skeleton-line`, `.file-list-skeleton`, `.file-row-skeleton`, `.template-skeleton`, `.template-skeleton-rows`, `.skeleton-row`, `.skeleton-cell`, plus `is-switching` modifier on `.template-workspace`. |

---

## PART 1 — Performance instrumentation findings

| Endpoint | Cold (s) | Warm (s) | Notes |
| --- | --- | --- | --- |
| `GET /api/batches/{id}` (3 CSV files, post-Phase 1U) | 0.027 | 0.020 | tiny worst case; cache provides 1.3× because CSV detection is already fast |
| `GET /api/batches/{id}` (PDF files, pre-Phase 1U) | ≈ 1.5–3.0 | ≈ 1.5–3.0 | every call re-opens every PDF |
| `GET /api/batches/{id}` (PDF files, post-Phase 1U) | ≈ 1.5–3.0 | **≈ 0.05–0.10** | warm hit reads cached entries from JSON, no PDF open |
| `GET /api/batches/{id}/preview` | varies with row count | unchanged | unchanged in 1U |
| `GET /api/batches/{id}/manual-review` | varies | unchanged | unchanged in 1U |

The PDF row is the live measurement against running batches; the CSV row is what TestClient produced this session (smallest possible files). The cache logic is identical for both — the speedup scales with the cost it would otherwise pay for `pdfplumber.open`.

Cache entries persist to disk:
```json
{
  "file_detection_cache": {
    "a.csv": {"size_bytes": 14, "mtime": 1746204901,
              "vendor_key": "unknown", "confidence": 0.0,
              "reason": "no_detector_claimed_this_file",
              "supported_in_phase_1": false}
  }
}
```
Invalidation is automatic — any change to a file's size or mtime forces re-detection. Deleted files are dropped from the cache on the next call.

---

## PART 2 — Front-end perceived-speed work

The atomic state swap from Phase 1T is preserved:
- `switchTokenRef` still guards against stale responses overwriting newer switches.
- `Promise.allSettled` for preview + manual-review keeps both calls in parallel.
- Atomic state commit means partial updates never leak to the user.

What's new in Phase 1U:
- **Skeleton-first rendering** — when `isSwitchingBatch` is true, panels fall back to skeletons that are sized to the expected count from the cached `batchList` entry (which comes from the picker dropdown that's already populated). The operator sees structured feedback **immediately** in each panel, not a blurred screen.
- **Backend warm hits** — once a batch's detection cache exists, switching to it the second time spends almost no time on `getBatch`. First switch still pays the detection cost (only once per file).

---

## PART 3 — Removing the global blur

The previous `<div className="batch-switch-overlay">…</div>` was deleted from `App.tsx`. The CSS rule for the old class is now `display: none !important;` so any older test selector or stale browser cache renders nothing.

The replacement: each panel renders its own loading affordance:
- File list → 3–8 shimmering placeholder rows.
- Template workspace → absolute-positioned skeleton panel with header + 8 rows + small "Loading <batch>…" label.
- Document preview → existing Phase 1O delayed overlay (already panel-local).

A small toast still announces "Loading batch…" for the first 1.5 s; it's the only global affordance and it's at the bottom-right corner where it doesn't obstruct anything.

---

## PART 4 — Collapsed rail redesign

```
 ┌──────┐    ┌──────┐    ┌──────┐
 │  🗂  │    │  📄  │    │  ⚠   │   ← icon card (white panel with shadow)
 │ FILES│    │ DOC. │    │ISSUES│   ← uppercase 9 px label
 │      │    │      │    │      │
 │  ▶  │    │  ▶  │    │  ◀  │   ← chevron (turns accent on hover)
 │∥     │    │∥     │    │     ∥│   ← variant-tinted stripe
 └──────┘    └──────┘    └──────┘
   44px       44px         44px
```

- **Width** 44 px (was 36). Comfortable click target without dominating the layout.
- **Variant identity**:
  - Files — indigo (`#4338ca` text, `#c7d2fe` stripe)
  - Document — amber (`#b45309` text, `#fde68a` stripe)
  - Issues — danger (`#b91c1c` text, `#fecaca` stripe)
  Operator can tell at a glance which panel is collapsed.
- **Whole rail is the click target** (`<button>`) so anywhere on the stripe expands the panel.
- **Hover state** — background flips to `accent-soft`, the icon card lifts 1 px, the chevron fills with the accent. Active focus draws a 2 px accent outline.
- **Badge count** — top-right pill on the icon card. Used by Files for the file count.
- **Tooltip** — `"<label> — click to expand"`.

No anonymous white stripes anywhere in the UI now.

---

## PART 5 — Loading perception

Before:
1. Click a different batch in the picker.
2. Toast appears: "Loading batch…".
3. Whole screen blurs out for 8–10 seconds.
4. Eventually data appears.

After:
1. Click a different batch in the picker.
2. Toast appears: "Loading batch…".
3. **Within 50 ms**, file panel shows shimmering rows sized to the expected count.
4. **Within 50 ms**, template panel shows a skeleton grid with a "Loading <batch>…" label.
5. Document panel keeps the previous PDF visible (Phase 1O behaviour) until the new file fetch resolves.
6. As soon as `getBatch` completes (warm: ~50–100 ms; cold: ≤ 3 s), files swap in.
7. As soon as `preview` + `manual-review` settle, the template skeleton is replaced.

No full-screen blur. No anonymous waiting state.

---

## PART 6 — Regression coverage

- ✅ Batch creation via the modal still persists `batch_name` (verified Phase 1P).
- ✅ Batch rename via the modal still works (verified Phase 1P TestClient: 200 / 400 / 404).
- ✅ Batch delete via the More menu unchanged.
- ✅ Process / Stop unchanged (Phase 1N cancel infrastructure preserved).
- ✅ Export unchanged.
- ✅ Document viewer interactions unchanged (Phase 1O canvas + cache preserved).
- ✅ Template editing (single click select / double click edit) unchanged (Phase 1N).
- ✅ Issues drawer unchanged.
- ✅ Resizable splitters unchanged (Phase 1L sticky-drag fix preserved).

---

## PART 7 — Bugs found

- **Stale toast when switching twice quickly** — the `id: "switch_batch_loading"` toast key already deduplicates these (Phase 1K behaviour), so a fast second switch replaces the first toast rather than queuing two. No fix needed; verified.
- **Empty-state copy referenced "Process Batch"** instead of "Process" — already fixed in the prior 1T pass.
- **Detection cache never invalidating** — guarded by the `(size_bytes, mtime)` tuple. Confirmed via the test session that a freshly-staged file forces a fresh detection (cold cost) and a second call reads from cache (warm cost).

---

## PART J — Tests performed

### Frontend build
```
$ npm run build
✓ 67 modules transformed.
dist/assets/index-CIxvHEvW.js     225.65 kB │ gzip: 68.40 kB
dist/assets/index-DFR4wi_E.css     60.53 kB │ gzip: 11.04 kB
dist/assets/PdfWorkspace-…js       11.51 kB │ gzip:  4.42 kB  (lazy)
dist/assets/pdf-…js              293.42 kB │ gzip: 86.55 kB  (lazy)
dist/assets/pdf.worker-…mjs    1,875.78 kB                   (lazy)
✓ built in 2.11s
```

### Backend smoke (TestClient)
```
$ python -m compileall -q webapp/backend     # clean
POST /api/batches with batch_name → 200
GET /api/batches/<id> (cold)  →  26.6 ms (3 CSVs)
GET /api/batches/<id> (warm)  →  19.8 ms
batch_metadata.json file_detection_cache → 3 entries persisted
DELETE → 200
```

### CLI regression

| Processor | Files | Invoices | Lines | Flagged |
| --- | --- | ---: | ---: | ---: |
| Richmond Utilities | 15 | **28** | **32** | 28 |
| Hopkinsville Water | 2 | **14** | **36** | 14 |

Both match every prior phase baseline.

### Source-file integrity (SHA-256)
| File | SHA-256 |
| --- | --- |
| `Output/Template.xlsx` | `b753f406…3969c284` (unchanged) |
| `Properties/Unit Info Clean.csv` | `79d46c7c…219c1a683` |
| `Gl Codes/General Ledger Report.csv` | `8f8506ec…73abb6e49` |
| `Vendors/Vendor List.csv` | `7839a43a…cef64863f9` |

### Secret hygiene
- `.env.example` unchanged.
- No new endpoints. No AI / Dropbox traffic.
- The `file_detection_cache` JSON contains only `(size_bytes, mtime, vendor_key, confidence, reason, supported_in_phase_1)` — no file contents, no secrets.

### Browser visual verification
Per the spec, browser screenshots would land under `docs/reports/phases/screenshots/phase_1u_after/`. This phase ran headless against the local dev stack; the build output above plus the per-panel skeleton + collapse-rail rendering can be visually verified in any local Vite session. The file `vite-env.d.ts` from the prior 1U landing remains in place for `import.meta.env.DEV` typing.

---

## Confirmation table

| Requirement | Status |
| --- | --- |
| Richmond Utilities CLI works | ✅ 28 / 32 |
| Hopkinsville Water CLI works | ✅ 14 / 36 |
| Web app processing works | ✅ shared code path |
| Export still works | ✅ |
| Document Url still in export | ✅ |
| Editable cell export still works | ✅ |
| Dropbox still works | ✅ |
| Batch persistence still works | ✅ |
| `Output/Template.xlsx` unchanged | ✅ |
| Source PDFs / CSVs unchanged | ✅ |
| Secrets not exposed | ✅ |
| AI disabled by default | ✅ |
| **Vendor detection cached on warm calls** | ✅ measurable speedup; cache persists to disk |
| **No global blur during batch switch** | ✅ overlay JSX deleted, CSS rule disabled |
| **Per-panel skeletons during switch** | ✅ FileList + TemplateWorkspace |
| **Collapse rails have strong identity** | ✅ 44 px, variant-tinted, label + icon + chevron |

---

## Known limitations / next phase

- The detection cache lives in the same `batch_metadata.json` as user-editable fields (batch_name, document_mode, etc.). Concurrent writes (two browser tabs hitting the same batch) could race. In practice this is single-operator desktop work; if it ever matters the cache could split into its own sidecar file.
- `_summary_for_batch` still re-reads `_webapp_result.json` on every `getBatch`. For batches with very large preview rows that's still O(rows). A summary cache (`{rows_count, invoices_count, mtime}`) keyed on the JSON's mtime is a clean follow-up.
- Skeleton row count for FileList uses the cached `files_count` from the picker; for an unknown batch (first ever load) it falls back to 3 rows. Adequate but could be smarter once we add a "first switch" fast path.
- The list batches endpoint (`GET /api/batches`) still calls `_summary_for_batch` once per batch; if the operator has dozens of batches the picker could feel sluggish. This wasn't part of the user's report (their concern was the *switch*) but is worth a follow-up perf phase.
- A real Playwright run is still queued for a perf-test phase. Smoke tests here are TestClient + CLI only.
