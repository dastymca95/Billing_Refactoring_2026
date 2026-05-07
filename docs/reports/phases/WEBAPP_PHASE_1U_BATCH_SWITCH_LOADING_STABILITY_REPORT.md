# Webapp Phase 1U — Batch Switch Loading Stability Report

**Date:** 2026-05-02
**Scope:** Frontend only. Vendor business logic, CLI, Dropbox, export, AI policy unchanged.

---

## Symptom

Switching from one batch to another took 5–6 seconds before the destination batch's files / template appeared. During that window the UI looked broken: the file list went empty, the template grid showed *"No data yet. Click Process Batch to populate the preview."*, and only after the network calls finished did the new batch's data paint.

## Root cause

`handleSwitchBatch` in `App.tsx` was **eager + sequential**:

```ts
// Phase 1N (broken):
const status = await api.getBatch(newId);     // ~150ms
setBatchId(status.batch_id);                  // ← UI commits NOW
setFiles(status.files);
setSelected(status.files[0]?.filename ?? null);  // ← triggers a fresh
                                                  //   /content fetch in
                                                  //   DocumentPreviewPanel
if (status.preview_available) {
  const prev = await api.preview(...);        // ~300–1500ms (sequential)
  const rev  = await api.manualReview(...);   // ~100–300ms  (sequential)
  setPreview(prev);
  setReview(rev.items);
}
```

Three problems compounded:

1. **Premature partial commit** — `setBatchId / setFiles / setSelected` ran *before* the new preview was fetched. The template grid received the new `batchId` but no preview, so it rendered the "No data yet" empty state for the duration of the API calls.
2. **Sequential API fetches** — `api.preview` and `api.manualReview` ran one after the other instead of in parallel. The user paid for the sum of both round-trips, not the max.
3. **No stale-response guard** — quick double-clicks on different batches could let the older switch's late-arriving `setPreview` overwrite the newer switch's data.

There was a **toast** that said "Loading batch…" (Phase 1N) but no actual UI affordance over the workspace — it didn't prevent the empty flash, it just narrated the empty flash.

## Fix

`handleSwitchBatch` rewritten in [`webapp/frontend/src/App.tsx`](../../../webapp/frontend/src/App.tsx) to:

- **Fetch in parallel.** `api.preview` and `api.manualReview` now race via `Promise.allSettled([...])`. Soft-failure of one (e.g. transient 502) does not block the other.
- **Atomic state swap.** No `setBatchId / setFiles / setPreview / setReview` call fires until every API call has settled. The previous batch's UI keeps rendering during the transition, so the operator never sees an empty / "No data yet" flash.
- **Translucent overlay.** While `isSwitchingBatch` is true, a centered loading card sits over the full `.layout`. The card uses the same dot-pulse animation as the PDF loading overlay (vocabulary consistency from Phase 1O). Backdrop is `rgba(248, 250, 252, 0.55)` + `backdrop-filter: blur(2px)` — the previous batch's UI is visible underneath.
- **Stale-response guard.** A monotonic `switchTokenRef` is bumped on every switch attempt. Every async checkpoint (`getBatch`, `Promise.allSettled`, `finally`) verifies the token is still current. Late responses for a now-cancelled switch are dropped silently — no overwrite, no toast.
- **Friendly error.** A failed switch keeps the previous batch's UI intact and surfaces a single toast: *"Could not load batch. Please try again."* No big red banner, no clobbered state.
- **Empty-state copy** updated in [`webapp/frontend/src/components/ResManTemplatePreview.tsx`](../../../webapp/frontend/src/components/ResManTemplatePreview.tsx): *"No template rows yet. Click **Process** to populate the preview."* (was "Click Process Batch", which referred to a button label that hasn't existed since Phase 1L).
- **Dev-mode timing log.** `import.meta.env.DEV` gates two `console.debug` lines ("[switch] getBatch Xms" and "[switch] total Yms") so future regressions can be diagnosed quickly.

## Files modified

| File | Change |
| --- | --- |
| [`webapp/frontend/src/App.tsx`](../../../webapp/frontend/src/App.tsx) | Rewrote `handleSwitchBatch`. Added `isSwitchingBatch`, `loadingBatchName`, `switchTokenRef`. Wrapped the `.layout` with a `switching-batch` modifier class. Mounted a `.batch-switch-overlay` while in flight. |
| [`webapp/frontend/src/components/ResManTemplatePreview.tsx`](../../../webapp/frontend/src/components/ResManTemplatePreview.tsx) | Empty-state copy updated. |
| [`webapp/frontend/src/styles.css`](../../../webapp/frontend/src/styles.css) | Phase 1U section: `.batch-switch-overlay` (full-layout translucent layer), `.batch-switch-card` (rounded pill with dot-pulse + label), `.layout.switching-batch .actions-bar button[disabled]` rule (extra dim while switching). |
| [`webapp/frontend/src/vite-env.d.ts`](../../../webapp/frontend/src/vite-env.d.ts) | New file — adds `vite/client` ambient types so the dev-mode `import.meta.env.DEV` references compile. |

No backend code touched. No CLI behaviour changed.

## API call shape

The switch flow now issues exactly:

```
GET /api/batches/{id}                       (await)
GET /api/batches/{id}/preview                (parallel — only if preview_available)
GET /api/batches/{id}/manual-review          (parallel — only if preview_available)
```

For an unprocessed batch (`preview_available=false`), preview / manual-review are skipped entirely (`Promise.resolve(null)`), so switching to an empty batch finishes in roughly one round-trip.

## Stale-response guard

```ts
const switchTokenRef = useRef(0);
// inside handleSwitchBatch:
const token = ++switchTokenRef.current;
const isStale = () => token !== switchTokenRef.current;

const status = await api.getBatch(newId);
if (isStale()) return;
const [previewSettled, reviewSettled] = await Promise.allSettled([...]);
if (isStale()) return;
// commit state…
```

If the operator clicks Batch B while Batch A is still loading, Batch A's eventual response sees `isStale() === true` and returns without touching state. Only Batch B's response can commit.

## Empty-state semantics

| State | Old copy | New copy |
| --- | --- | --- |
| No preview yet (loading) | *"No data yet. Click **Process Batch**…"* | *(grid stays on previous batch's data; overlay shows "Loading <batch>…")* |
| Truly empty processed batch | *"No data yet. Click **Process Batch**…"* | *"No template rows yet. Click **Process** to populate the preview."* |
| Switch error | red workspace banner | toast: *"Could not load batch. Please try again."* |

## Performance logging (dev only)

```
[switch] getBatch 142ms
[switch] total 318ms
```

Only fires in development builds (`import.meta.env.DEV === true`). Production builds tree-shake the gated branches.

## Manual verification matrix

Smoke-tested via the FastAPI TestClient against the same endpoints the new flow consumes (a true browser run requires Playwright + a live backend; dev-server reachability was verified separately):

| Test | Expected | Result |
| --- | --- | --- |
| `GET /api/batches/<id>` for fresh batch | 200, `preview_available=false` | ✅ |
| `GET /api/batches/<id>/preview` before run | 404 | ✅ — the switch flow gates on `preview_available` so this 404 isn't hit |
| `GET /api/batches/<id>/manual-review` before run | 404 | ✅ — same gate |

The visual flow ("UI doesn't blank during switch", "overlay fades in / out") is verifiable manually at http://localhost:5174 once the backend is running on :8001 — see the README's *Stale backend reset runbook* if `Method Not Allowed` ever surfaces.

## Tests performed

### Frontend build
```
$ npm run build
✓ 67 modules transformed.
dist/assets/index-CdsZWEH-.js  224.44 kB │ gzip: 68.10 kB
dist/assets/index-B17oqgO5.css  57.61 kB │ gzip: 10.51 kB
dist/assets/PdfWorkspace-…js    11.51 kB │ gzip:  4.41 kB  (lazy)
✓ built in 1.57s
```

### Backend
- `python -m compileall -q webapp/backend` — clean, no compile errors.
- TestClient round-trip on `POST /api/batches`, `GET /api/batches/<id>`, `GET preview`, `GET manual-review` — all expected status codes.

### CLI regression

| Processor | Files | Invoices | Lines | Flagged |
| --- | --- | ---: | ---: | ---: |
| Richmond Utilities | 15 | **28** | **32** | 28 |
| Hopkinsville Water | 2 | **14** | **36** | 14 |

Both match every prior phase baseline.

### Source-file integrity (SHA-256)

| File | SHA-256 |
| --- | --- |
| `Output/Template.xlsx` | `b753f406…3969c284` (unchanged across every phase) |
| `Properties/Unit Info Clean.csv` | `79d46c7c…219c1a683` |
| `Gl Codes/General Ledger Report.csv` | `8f8506ec…73abb6e49` |
| `Vendors/Vendor List.csv` | `7839a43a…cef64863f9` |

### Secret hygiene
- `.env.example` unchanged.
- No new endpoints. No AI / Dropbox traffic.

---

## Known limitations

- **Document preview re-render** still happens when `selected` (the active filename) changes after the swap. The Phase 1O delayed-overlay + offscreen-render fix already keeps that visually stable, but for a **very large** new file the document panel may briefly show "Loading document…" — separate from the batch-switch overlay. That's correct behaviour: batch-switch is over by then; what's loading is the new document.
- **Region hints** are still loaded by `PdfWorkspace` on its own (per Phase 1H). The batch-switch flow doesn't preload them, so a fast switch into Mark Fields mode may briefly show "Saving regions…" / empty state until the PUT round-trips. Pre-loading them on switch is queued for a perf phase.
- The `loadingBatchName` falls back to the literal word "batch" for the (rare) case where the destination batch isn't in the cached `batchList` yet. This happens on first load before the picker has been opened. Once the picker is opened once, the cache is populated.
- The overlay does not block keyboard tab focus — actions inside the previous batch's UI are still focusable but most are disabled while `isSwitchingBatch` is true (handled at the button level, not via `pointer-events`).
- True end-to-end timing data requires a Playwright run with two batches; that infrastructure isn't wired up yet (PART H screenshots deferred). The dev-mode `console.debug` lines provide enough for an operator to grab timings during local debugging.
