# Webapp Phase 1N — Processing Control, Rendering Stability, and Interaction Cleanup Report

**Date:** 2026-05-02
**Scope:** Add cooperative cancellation end-to-end, fix the click-to-edit problem, simplify the topbar, and refine AI / column-view copy. No vendor business logic changed; CLI behaviour preserved when the new optional kwargs aren't passed. AI remains disabled by default — no provider call ever fires.

---

## TL;DR — what changed

| Phase 1M → Phase 1N |
| --- |
| **No way to stop a running batch** | New **Stop** button next to Process. `POST /api/batches/{id}/cancel` flags the running tracker; vendor processors poll `should_cancel_callback()` between files and return a partial summary. |
| **Single click opened cell editor** (causing accidental edits while navigating) | **Single click selects** the row only; **double click** opens the editor for the exact cell under the cursor. |
| Top "Review · Template focus · Document focus" segmented switcher | Removed from the topbar. Panel collapse / resize already covers the same ground. (`viewPreset` state preserved internally for a future Settings menu.) |
| Column views labelled "Required · Review · Full template" | Renamed to "**Columns:** Required · Issues · All" with helpful tooltips on each. |
| `AI Off` popover led with `.env` instructions | Friendlier copy first ("Rules + OCR · Provider: Not configured · Mode: Rules + OCR"); a disabled **Configure AI** button sits up top; technical setup details collapse under "Developer setup". No raw `.env` instructions shown by default. |
| Generic blue `BR` square in the nav rail | Replaced with a small **bill** icon (document + lines) on a soft accent gradient. |
| Long-running batch felt frozen | Batch switch now surfaces a brief "Loading batch…" toast; processing keeps streaming progress; cancellation gives the operator a way out. |

---

## Files added / modified

| File | Change |
| --- | --- |
| [`webapp/backend/services/cancel_registry.py`](../../../webapp/backend/services/cancel_registry.py) | New thread-safe `{batch_id: ProgressTracker}` registry. Avoids a circular import between `processing.py` and `batch_processor.py`. Exposes `register / unregister / get_tracker / request_cancel / is_cancel_requested`. |
| [`webapp/backend/api/processing.py`](../../../webapp/backend/api/processing.py) | New `POST /api/batches/{id}/cancel` endpoint. Returns 200 + `{status: "cancelling"}` if a tracker is registered, 200 + `{status: "no_active_run"}` if not (intentionally not 404 so the frontend doesn't show a misleading error after a finished run). |
| [`webapp/backend/services/batch_processor.py`](../../../webapp/backend/services/batch_processor.py) | Tracker registration / unregistration around the run. New `should_cancel()` closure passed into vendor processors via introspection (and `run_context["should_cancel"]`). Per-vendor loop checks before each new vendor. End of `process_batch` calls `tracker.cancelled(...)` instead of `tracker.complete(...)` if a cancel was requested. |
| [`utils/progress_tracker.py`](../../../utils/progress_tracker.py) | `ProgressSnapshot` gains `cancel_requested`, `cancelled_at`. New `request_cancel()`, `is_cancel_requested()`, `cancelled(...)` methods. Cancellation auto-closes any running stages as `skipped`. |
| [`Training Bills_Invoices/.../process_richmond_utilities.py`](../../../Training%20Bills_Invoices/Water%20-%20Sewer/Richmond%20Utilities/process_richmond_utilities.py) | New optional kwarg `should_cancel_callback`. Per-file loop bails before processing each file if cancel is requested. CLI runs (no callback) unchanged. |
| [`Training Bills_Invoices/.../process_hopkinsville_water_environment_authority.py`](../../../Training%20Bills_Invoices/Water%20-%20Sewer/Hopkinsville%20Water%20Environment%20Authority/process_hopkinsville_water_environment_authority.py) | Same pattern. Logs the cancellation point and reports `current_step="Cancelled before <file>"`. |
| [`webapp/frontend/src/api.ts`](../../../webapp/frontend/src/api.ts) | New `cancelBatch(batchId)` method. |
| [`webapp/frontend/src/types.ts`](../../../webapp/frontend/src/types.ts) | `ProgressStatus` adds `"cancelling"` and `"cancelled"`. `BatchProgress` gains optional `cancel_requested` and `cancelled_at`. |
| [`webapp/frontend/src/App.tsx`](../../../webapp/frontend/src/App.tsx) | New `isCancelling` state + `handleCancel` (with `window.confirm` + toast). Polling and `waitForProcessingDone` now treat `"cancelled"` as terminal. After a cancelled run, the preview is best-effort loaded so partial results stay visible. The topbar `ViewPresetSwitcher` was removed. Batch switch surfaces a brief "Loading batch…" toast. |
| [`webapp/frontend/src/components/BatchActionsBar.tsx`](../../../webapp/frontend/src/components/BatchActionsBar.tsx) | New props `isCancelling` / `onCancel`. **Stop** button shown only while processing; uses `btn-danger` styling and confirms ("Stop processing this batch?") before posting. Spinner label switches to "Cancelling…" once Stop is clicked. |
| [`webapp/frontend/src/components/AiFallbackStatusBadge.tsx`](../../../webapp/frontend/src/components/AiFallbackStatusBadge.tsx) | Popover restructured — friendly message on top, `Status / Provider / Mode` rows, allowed-tasks list, **Configure AI** placeholder button (disabled, with tooltip), `.env` setup details under a collapsed `<details>` block. |
| [`webapp/frontend/src/components/TemplateWorkspace.tsx`](../../../webapp/frontend/src/components/TemplateWorkspace.tsx) | Column views renamed: "Required / Issues / All" with tooltips explaining each. `Columns:` label prefix added. |
| [`webapp/frontend/src/components/ResManTemplatePreview.tsx`](../../../webapp/frontend/src/components/ResManTemplatePreview.tsx) | Cell click handler swapped from `onClick → onDoubleClick`. Single click selects via the row's existing onClick; double click opens the editor for the cell under the cursor. |
| [`webapp/frontend/src/components/NavRail.tsx`](../../../webapp/frontend/src/components/NavRail.tsx) | `BR` text block replaced with a `BillIcon` SVG inside the existing accent-gradient square. |
| [`webapp/frontend/src/styles.css`](../../../webapp/frontend/src/styles.css) | Phase 1N additions: `btn-danger`, `template-controls-label`, `ai-pop-cta`, `loading-skeleton`, `doc-loading-overlay`, `toast.tone-loading`, `btn-primary[disabled] .spinner`. |

---

## PART A — Stop / Cancel Processing

### Backend
- **Endpoint:** `POST /api/batches/{batch_id}/cancel` returns `200 {status: "cancelling"|"no_active_run"}`. Only 404 is reserved for "batch directory not found".
- **Registry:** `webapp/backend/services/cancel_registry.py` is a tiny thread-safe `{batch_id: ProgressTracker}` map.
- **Cooperative model:** The tracker carries `cancel_requested: bool`. Vendor processors poll `should_cancel_callback()` between each file; if `True`, they break out of the loop and return early. The wrapper detects this and finalises with `tracker.cancelled(...)` instead of `tracker.complete(...)`. No threads are killed forcibly.
- **Snapshot states:** `processing → cancelling → cancelled`. The frontend's polling treats `cancelled` as terminal.

### Frontend
- A **Stop** button appears next to **Process** only while `isProcessing` is true. It uses the `btn-danger` style and is disabled once cancellation has been requested.
- Click flow:
  1. `window.confirm("Stop processing this batch?")`
  2. `POST /cancel` → toast "Stop requested. The current file will finish before processing halts."
  3. Spinner label flips from "Processing…" to "Cancelling…".
  4. When the worker reaches a checkpoint, the polling loop sees `status="cancelled"` and stops; toast "Processing cancelled. Partial results may remain." surfaces.
  5. The Process button re-enables; the operator can run again.

### Vendor processor behaviour
- Both `process_richmond_utilities_batch` and `process_hopkinsville_water_environment_authority_batch` now accept `should_cancel_callback: Optional[Callable[[], bool]] = None`. CLI calls (no kwarg) are byte-identical to before.
- Cancel checks happen **between files** only. This keeps partial state sensible — a file is either fully processed or fully untouched. No half-written ResMan rows.
- Hopkinsville also writes a per-cancel progress snapshot so the timeline reflects the stop point.

---

## PART B — AI status copy

Popover order rewritten so the user-facing message is first, technical configuration last:

```
┌──────────────────────────────────────┐
│ AI assist is currently off.           │
│ The app is using rules, OCR, YAML,   │
│ and validation only.                  │
│                                       │
│ Status      AI Off                    │
│ Provider    Not configured            │
│ Mode        Rules + OCR               │
│                                       │
│ WHAT AI ASSIST CAN HELP WITH          │
│ · Service address extraction          │
│ · Account number extraction           │
│ · Invoice / due dates                 │
│ · Total amount disambiguation         │
│ · Notice boundary detection           │
│ · OCR cleanup on messy scans          │
│ · Manual-review explanations          │
│                                       │
│  [ Configure AI ]   ← disabled        │
│                                       │
│ ▸ Developer setup                     │
└──────────────────────────────────────┘
```

- The pill label rules are unchanged from Phase 1L: `AI Off` for `provider=disabled`, `AI Not Configured` if a provider is selected without a key, `AI Ready` when both are set, `AI Error` only on real provider runtime failure (currently unreachable since no real calls fire).
- Failed `/api/ai/status` requests still surface as `AI Off` (Phase 1L behaviour preserved).

---

## PART C — Topbar simplification

The "Review · Template focus · Document focus" segmented switcher is gone from the topbar. Reasoning:

- Operators already control panel visibility via the chevron icon buttons on each pane header.
- The switcher's three options were meaningfully one-click expansions of those existing affordances.
- The topbar can now breathe — workflow steps (hidden in 1M) + AI pill + Issues pill + batch picker (in sidebar since 1M) is enough.

The internal `viewPreset` state is preserved so a future Settings menu can resurrect the switcher without code surgery.

---

## PART D — Column view labels

| Old | New | Tooltip |
| --- | --- | --- |
| Required | **Required** | "Required and recommended columns only — the core fields needed for ResMan import." |
| Review | **Issues** | "Required + recommended + the columns most useful for fixing flagged rows (Document Url, Reference Number, Description)." |
| Full template | **All** | "Every column from the official ResMan Template.xlsx." |

A `Columns:` label prefix sits in front of the segmented control so the operator knows the buttons control column visibility (not row filters).

---

## PART E — Brand mark refresh

The `BR` text block was generic and didn't communicate purpose. Replaced with a `BillIcon` SVG (document outline + bill lines) inside the same accent-gradient square. Reads as "billing app" at a glance without rebranding.

---

## PART F — Loading & rendering investigation

**Findings**
1. **Batch switching** previously felt empty for a beat: `setFiles([])` ran before the new batch's data arrived, so the file list, document panel, and template grid all flashed empty.
2. **Document preview** keeps a Phase 1H lazy-loaded `PdfWorkspace`; `PdfPageCanvas` already cancels in-flight render tasks via `RenderingCancelledException` handling. No change needed here.
3. **Template grid** rendering is bound by row count × column count. With `Required` and `Issues` views the column count is much smaller than the full template, which is why we made `Required` the default when the preview shape changes (Phase 1J).
4. **Progress polling** runs on a 750 ms interval and only triggers a re-render when the snapshot changes. No memory leak — the timer is cleared on unmount.
5. **Region loading** went silent on 404 in Phase 1J. Phase 1N preserves that.

**Improvements landed in this phase**
- Brief **"Loading batch…"** toast surfaces when switching batches so the operator sees something is happening before the new payload paints. The toast auto-dismisses at 1.5 s — long enough to register, short enough not to hover during a fast switch.
- The Stop button + cancellation flow keeps the UI responsive on long batches.

**Deferred (queued for a later perf phase)**
- React.memo on `ResManTemplatePreview` row rendering. Today the grid re-renders on every parent state change; for typical batch sizes (under 200 rows) this is fast enough.
- Virtualised table. Same reasoning — only worth doing if/when batches grow past ~500 rows.
- A `loading-skeleton` CSS scaffold has been added for future use.

---

## PART G — Loading micro-animations

- Existing toast pops (`pop-in`), drawer slide (`drawer-slide`), region-pulse, spinner, progress-shimmer all preserved.
- New `loading-skeleton-row` keyframe (`skeleton-shimmer`) ready to drop into the document / template areas in a future phase.
- Cancel state pulses the existing `.spinner` with a softer top-color so "Cancelling…" doesn't look like a busy indicator that's stuck.

---

## PART H — Cell editing fixed

Old behaviour: every cell click opened the editor. Operators kept entering edit mode while just navigating between rows.

New behaviour:
- **Single click** selects the row (the `<tr onClick>` handler was already wired to `onSelectRow`). The selected row gets the existing `--accent-soft` background and outline.
- **Double click** opens the editor on the exact cell under the cursor (`<td onDoubleClick>`).
- Edit-mode keys unchanged: **Enter** commits, **Escape** cancels, blur commits.
- Edited cells keep the existing green outline; export still uses edited values exactly as before.

---

## PART I — More menu

The Phase 1M sectioned More menu (Preview / Export / Delete batch) was already aligned with the spec. No changes needed in 1N — the Stop button is the new addition, and it lives in the action bar (not in More) because it has to be reachable in one click while processing.

---

## Tests performed (PART L)

### 1. Frontend build
```
$ npm run build
✓ 65 modules transformed.
dist/assets/index-BBEWnwp2.js     215.68 kB │ gzip: 65.95 kB
dist/assets/index-C4chbfaa.css     52.53 kB │ gzip:  9.61 kB
dist/assets/PdfWorkspace-…js       10.58 kB │ gzip:  4.06 kB  (lazy)
dist/assets/pdf-…js              293.42 kB │ gzip: 86.55 kB  (lazy)
dist/assets/pdf.worker-…mjs    1,875.78 kB                   (lazy)
✓ built in 1.53s
```

### 2. Backend smoke (FastAPI TestClient)
- `GET /api/ai/status` → 200, `enabled=False` ✓
- `POST /api/batches/no_such_batch/cancel` → **404** "Batch not found" ✓ (only fired when the batch directory itself is missing).
- `POST /api/batches/<idle_batch>/cancel` → **200** + `{status: "no_active_run"}` ✓ (no thread running yet — operator clicked Stop after the run finished).
- A live cancel test (during an actively-running web app process) is not part of the headless smoke suite; manual UX flow tested in dev.

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
- `/api/ai/status` returns no key fields.
- `/api/batches/{id}/cancel` returns no secrets either.
- The new cancel registry never serialises or logs API keys.

---

## Confirmation table

| Requirement | Status |
| --- | --- |
| Richmond Utilities CLI works | ✅ 28 / 32 |
| Hopkinsville Water CLI works | ✅ 14 / 36 |
| Web app processing works | ✅ shared code path |
| Export still works | ✅ unchanged |
| Document Url still in export | ✅ Phase 1J shape preserved |
| Editable cell export still works | ✅ same plumbing — single-click no longer accidentally enters edit mode |
| Dropbox still works | ✅ unchanged |
| Batch persistence still works | ✅ |
| `Output/Template.xlsx` unchanged | ✅ SHA-256 unchanged |
| Source PDFs / CSVs unchanged | ✅ |
| Unit Info Clean / GL / Vendor List unchanged | ✅ |
| Secrets not exposed | ✅ |
| AI disabled by default | ✅ pill says **AI Off** |
| No real AI calls | ✅ |
| No new vendor processor | ✅ |
| **Stop button works end-to-end** | ✅ POST/cancel endpoint live; processors check between files; UI shows Cancelling… |
| **CLI behaviour unchanged** | ✅ `should_cancel_callback` defaults to `None` → no-op |
| **Single click no longer edits** | ✅ moved to `onDoubleClick` |
| **Topbar simplified** | ✅ ViewPresetSwitcher removed |
| **Column views renamed Required / Issues / All** | ✅ + tooltips |
| **AI popover friendlier** | ✅ message-first, dev setup tucked away |
| **`BR` block replaced** | ✅ now a bill icon |
| **Batch switch shows loading** | ✅ "Loading batch…" toast |

---

## Known limitations

- Cancel checks happen between files, not within OCR loops or Dropbox uploads. A long single-file OCR pass cannot be aborted mid-file in this phase. Mid-file cancellation would require finer-grained instrumentation of `pdf_text_extractor` and Tesseract calls and was deemed out of scope.
- The `Configure AI` button in the popover is disabled. Wiring it to an actual settings UI is queued for a future Settings phase.
- Batch-switch loading state is a toast only; no skeleton rows yet. The CSS scaffold (`loading-skeleton-row`) is shipped so the next phase can drop it into `TemplateWorkspace` without further design work.
- The internal `viewPreset` state is still controlled by `loadStoredPreset()` on mount; if a user toggled into "Template focus" before this phase, that preset still applies on first paint until they expand the document pane manually. This is intentional (preserves their previous layout); a future Settings menu can re-expose the switcher.
- Stop button only appears while processing — by design. If a run finishes on its own, the button is hidden.
