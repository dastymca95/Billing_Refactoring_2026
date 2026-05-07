# Phase 2D — Template Identity, Revision History & Cross-Batch Queue

**Date:** 2026-05-03
**Scope:** Workspace shell upgrade. Each major panel (Batches, Document, Template) gets desktop-style window controls (— □ ×). Equal-height alignment is preserved; closed panels live in a small "Restore" dock. Template identity is finally separate from the batch name. Every successful Process produces a frozen revision the operator can browse + activate. Multiple batches can be queued — only one runs at a time.

---

## 1. Workspace shell — what changed

The old Template header carried a tiny 3-icon cluster (popout document, popout template, focus toggle) that the user rightly called out as misplaced. Phase 2D moves window controls into the chrome of *each* major panel:

| Panel | Minimize | Maximize | Close |
|---|---|---|---|
| Batches sidebar | collapses to slim icon rail (existing behaviour) | hides Document + Template | hides panel; chip in topbar dock |
| Document Viewer | collapses to slim icon rail | hides Batches + Template | hides panel; chip in topbar dock |
| Template | hides itself; chip in topbar dock | takes over the workspace row | hides panel; chip in topbar dock |

Closed panels surface as chips in a small "Restore" toolbar in the topbar (visible only when something is closed). Click the chip to reopen.

`focusModeTemplate` from Phase 2C is still wired but the equivalent is now reachable from the Template panel's maximize button — there is one canonical control per behaviour.

## 2. Module controls architecture

`webapp/frontend/src/components/PanelHeader.tsx` (new) — a presentational `<PanelHeader>` with `onMinimize / onMaximize / onClose` callbacks and `isMinimized / isMaximized` flags. The actual headers live inside each panel today (file-sidebar, DocumentPreviewPanel, TemplateWorkspace) so we render the same `.panel-window-controls` cluster inline in each panel rather than swapping their markup wholesale. The component is available for future panels (e.g. an inspector) and the styling is shared.

`App.tsx` owns layout state:
```ts
const [closedPanels, setClosedPanels] = useState<Set<"batches" | "document" | "template">>();
const [maximizedPanel, setMaximizedPanel] = useState<"batches" | "document" | "template" | null>();
```
The `.layout` element gains `panel-closed-batches | panel-closed-document | panel-closed-template` and `module-max-batches | module-max-document | module-max-template` classes. CSS hides the right panels + their adjacent resizers when any of these is set. Panel sizes are not mutated — when the operator restores from the dock, `useResizablePanel` already has the previous width pinned in localStorage, so layout snaps right back.

## 3. Equal-height layout

The three major panels have always been flex children of `.layout` so they share height already. The visible mismatch came from inner chrome (different headers, different scroll containers). Phase 2D unifies the per-panel header strip via a shared `.panel-window-controls` ruleset (22-px buttons, no borders, hover pill, close-red on hover) and tightens the panel-header padding to 6px/10px. Resizers between adjacent panels are preserved exactly as before, including the Phase 1L sticky-resize fix.

## 4. `export_name` vs `batch_name` vs `revision_id`

| Field | Owner | Purpose | Example |
|---|---|---|---|
| `batch_name` | `batch_metadata.json::batch_name` | Operational batch label | "Richmond 3" |
| `export_name` | `batch_metadata.json::export_name` (Phase 2C–D) | Operator-chosen filename for the workbook download | "Richmond Utilities March 2026 Import.xlsx" |
| `revision_id` | `revisions/index.json` per batch | One specific frozen process result | "rev_20260503T201652Z" |

The Template title shows the **export filename** (the actual one the workbook will download as — see Phase 2D download-CD wiring). The breadcrumb shows the **batch name**. The Revisions dropdown shows the **revision history** for the active batch.

The placeholder treatment is no longer italic-muted — when no `export_name` is saved, the title shows the *real default* (`<batch_name>_ResMan_Import.xlsx`, computed by the backend) in clean semibold. The helper line under the title (Phase 2D) explains that this is the download filename.

## 5. Revision history model

Every successful process run produces:

- A frozen JSON snapshot of the result cache: `webapp_data/batches/<batch_id>/revisions/rev_<UTC>.json`
- An entry prepended to `webapp_data/batches/<batch_id>/revisions/index.json` carrying:
  - `revision_id`
  - `created_at` (UTC ISO)
  - `status` (`completed` for the happy path)
  - `export_name` snapshot (whatever was saved at the moment of the run)
  - `files_count`, `invoices_count`, `rows_count`, `manual_review_count`
  - `source_batch_id`
  - `snapshot_filename` (basename only)

`processed/_webapp_result.json` remains the *active* revision — it's what `/preview` and `/manual-review` read. Activating an older revision copies its snapshot back over the active cache; no other endpoints had to change.

API:

| Route | Behaviour |
|---|---|
| `GET /api/batches/{id}/revisions` | List the manifest (newest first) + `current_revision_id` (the newest by default) |
| `POST /api/batches/{id}/revisions/{rev_id}/activate` | Copy the snapshot over the active cache; 404 on missing, 400 on bad id |

Path traversal is double-defended: the `revision_id` regex (`rev_[0-9TZ_-]+`) plus a `relative_to(revisions/)` check inside the service.

UI: `<RevisionsDropdown>` lives in the Template header. Shows `Revision · v3 ▾`, opens to a list:
```
●  v3 — 2026-05-03 14:15  · 14 invoices · 16 rows · 14 issues       Current
○  v2 — 2026-05-03 13:48  · 14 invoices · 16 rows · 14 issues
○  v1 — 2026-05-02 19:09  · 10 invoices · 63 rows · 10 issues
```
Clicking activates the chosen revision; the preview re-fetches in place.

## 6. Queue model

A single global worker thread runs at most one batch at a time:

- Submitting `/process` for an idle queue starts immediately.
- Submitting while a batch is running adds the new batch to a FIFO.
- Re-submitting an already-running or already-queued batch is a no-op (returns the existing position).
- Cancelling a queued (not yet started) batch removes it from the FIFO.
- Cancelling the running batch routes through the existing `cancel_registry.request_cancel(batch_id)` so the vendor processor stops at its next checkpoint.
- When the running batch finishes, the worker pulls the next item from the FIFO automatically.

Service: `webapp/backend/services/processing_queue.py` (new) — single-runner pattern, `threading.RLock` + condition variable, daemon worker thread spun up lazily on first submit.

API:

| Route | Behaviour |
|---|---|
| `POST /api/batches/{id}/process` | Now goes through `processing_queue.submit(batch_id, _run_batch_in_background)`. Response is `{status: "accepted", queue: {state, position, running, queued}, polling_url}` |
| `POST /api/batches/{id}/cancel` | First tries `processing_queue.cancel`; falls back to legacy tracker flag |
| `GET /api/processing/queue` | `{running: batch_id\|null, queued: [batch_ids]}` |

Concurrency invariants verified by smoke (Section 9): submitting A → B → C and cancelling C means the runner only sees A and B; final queue is empty.

UI: a small chip on each Batch row in the BatchExplorer:

| Live state | Chip |
|---|---|
| running | blue `Running` with pulse dot |
| queued | amber `Queued` with slow pulse dot |
| completed (from `BatchListEntry.status`) | green `Done` |
| failed | red `Failed` |
| cancelled | red `Cancelled` |
| idle | no chip |

The frontend polls `/api/processing/queue` every 1.5 s and feeds the snapshot to BatchExplorer; per-batch state is derived inline.

## 7. Frontend changes

- `src/components/PanelHeader.tsx` — new presentational component with — □ × controls.
- `src/styles.css` — Phase 2D block: panel-header strip, panel-window-btn, module-dock chips, panel-closed-* / module-max-* layout overrides, batch-queue-chip, revisions-popover (animated). The Phase 2C.1 italic placeholder rule is gone.
- `src/main.tsx` — unchanged.
- `src/App.tsx` —
  - `closedPanels` + `maximizedPanel` state and the layout-class wiring.
  - `revisions` + `currentRevisionId` state + `refreshRevisions(bid)`.
  - `queueStatus` polled every 1.5 s.
  - `handleActivateRevision` — calls `api.activateRevision`, re-fetches preview + manual-review.
  - Module dock JSX rendered in the topbar.
  - Window-control buttons rendered in the file-sidebar header (sidebar still uses inline buttons rather than wholesale-replacing its chrome with `<PanelHeader>` to avoid disrupting the BatchActionsBar layout).
- `src/components/BatchExplorer.tsx` — new `queueStatus` prop; per-row queue chip; `BatchRow` accepts `queueState`.
- `src/components/DocumentPreviewPanel.tsx` — new `onMaximize / onClose / isMaximized` props; — □ × cluster replaces the legacy single collapse arrow.
- `src/components/TemplateWorkspace.tsx` —
  - `<RevisionsDropdown>` (new) lives next to the KPI cluster.
  - Title placeholder no longer italic; clean semibold.
  - The 3-icon cluster (popout / focus) is replaced by — □ × window controls.
  - `onMinimize / onMaximize / onClosePanel` props plumbed from App.
- `src/api.ts` — `listRevisions`, `activateRevision`, `getQueueStatus`.
- `src/types.ts` — `RevisionEntry`, `RevisionListResponse`, `QueueStatus`.

## 8. Backend changes

- `webapp/backend/services/revisions.py` (new) — `record_revision`, `list_revisions`, `activate_revision`, `current_revision_id`. Path-traversal-safe; rev_id regex pinned.
- `webapp/backend/services/processing_queue.py` (new) — `submit`, `cancel`, `status`, `state_for`, `is_running`, `is_queued`, `all_states`. Persistent worker thread.
- `webapp/backend/api/processing.py`:
  - `_run_batch_in_background` now records a revision after the cache write.
  - `process_endpoint` async path goes through `processing_queue.submit`.
  - sync path also records a revision.
  - `cancel_endpoint` first tries the queue (so queued batches can be removed), then falls back to the legacy tracker flag.
  - Two new endpoints: `GET /{batch_id}/revisions`, `POST /{batch_id}/revisions/{rev_id}/activate`.
  - A new `queue_router` exposes `GET /api/processing/queue`.
- `webapp/backend/main.py` — includes `processing.queue_router`.
- `batch_metadata.json` schema is unchanged. `export_name` (Phase 2D earlier) is still the source of truth for the download filename. Revisions live in their own `revisions/` directory, not in metadata, so older batches without revisions remain valid.

## 9. Tests + integrity

| Check | Result |
|---|---|
| `npm run build` | ✓ 68 modules, 273.60 kB JS / 105.75 kB CSS |
| `npx tsc --noEmit` | ✓ no type errors |
| `python -m compileall webapp/backend` | ✓ no errors |
| Backend smoke — empty queue | `{running: None, queued: []}` |
| Backend smoke — sync process | 200, 10 invoices, revision recorded |
| Backend smoke — revisions list grows | 3 revisions after the run (existing 1 + new sync-run + earlier test runs) |
| Backend smoke — activate older revision | 200, `current_revision_id` swaps |
| Backend smoke — path traversal on activate | 404 (route mismatch + regex defence) |
| Backend smoke — submit A→B→C, cancel C | runner saw A and B only; final queue empty |
| Backend smoke — cancel idle batch | `no_active_run` |
| Backend smoke — cancel queued batch | `removed_from_queue` |

**Integrity invariants** (SHA-256, first 16):

| File | SHA | Status |
|---|---|---|
| `Output/Template.xlsx` | `b753f406c0222f15` | unchanged |
| `Vendors/Vendor List.csv` | `7839a43a493a7c0c` | unchanged |
| `config/vendors/hopkinsville_water_environment_authority.yaml` | `e83c554709edd0bf` | unchanged |
| `config/vendors/richmond_utilities.yaml` | `6111d042658818d4` | unchanged |
| `config/vendors/backups/` | empty | unchanged |

No vendor processors edited, no Dropbox calls, no AI calls, no source PDFs/CSVs touched, no `.env` changes.

`npm run test:e2e` was not executed — the existing Playwright suite predates Phase 2C/2D and would need new specs for the revision dropdown, queue chips, and module window controls.

## 10. Limitations + recommended next phase

**Limitations**
- The Batches sidebar's window controls live inside its existing header bar rather than via the new `<PanelHeader>` wrapper (avoiding disruption to BatchActionsBar). Visually consistent with the other panels, but the markup paths differ.
- `closed` panels do not animate out — they snap. Adding a fade/slide is straightforward in CSS once the layout is verified.
- Activating an older revision overwrites the active `_webapp_result.json` with the snapshot. If the operator had unsaved cell edits in the table, those are local React state and will *not* be lost (they live in `App.tsx::edits`), but the underlying preview rows will change. A confirm dialog before activate would be a polite v2.
- The queue chip in BatchExplorer reads live state for `running`/`queued` and persisted `BatchListEntry.status` for terminal states (`completed`/`failed`/`cancelled`). The persisted status is bumped by the existing `_write_metadata` flow; no schema migration needed.
- Cross-window queue updates aren't broadcast — the popout windows from Phase 2C will not re-render queue chips because they don't poll. They were intentionally read-only.
- Two screenshots are pending the Chrome extension reconnect; the local stack is up at `http://localhost:5174` and ready for capture.

**Phase 2E (recommended)**
1. Confirm dialog before activating an older revision when there are unsaved edits.
2. Per-revision delete (with retention policy: keep last N).
3. Live queue updates pushed via SSE so the BatchExplorer doesn't poll.
4. Playwright e2e specs for: the three module window-control flows, the queue chips, the revisions dropdown.
5. Migrate the Batches sidebar header to `<PanelHeader>` so all three panels share one component.
6. Surface the queue position badge on the active batch in the topbar (e.g. "Queued · 2 of 3") — useful when many batches are stacked.
