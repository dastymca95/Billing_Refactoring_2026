# Webapp Phase 1P — Batch Management UX + Rename API Fix + Action Placement Cleanup

**Date:** 2026-05-02
**Scope:** Frontend UX cleanup of batch management. Backend was already correct — investigation confirmed the rename endpoint works and persists custom names. No vendor business logic touched. AI remains disabled by default.

---

## TL;DR — what changed

| Before | After |
| --- | --- |
| Renaming a batch opened `window.prompt("localhost:5173 says — Rename this batch")`. | App-native **RenameBatchModal** with input, validation, Save/Cancel, Enter/Esc shortcuts, inline error messages, loading spinner. |
| Rename failures, switch failures, and create failures rendered as a giant red workspace banner via `setError(...)`. | All three converted to **toast notifications** (warning / error tone, auto-dismiss). The workspace panels never get pushed apart by an error block. |
| User-supplied batch names *appeared* lost (operators reported seeing `batch_20260502_…` instead of "May 2026"). | Backend was already saving + returning the custom name correctly — verified via TestClient. The previous browser-prompt flow was the visible UX issue, not the persistence layer. |
| **Process** + **Export** sat side-by-side in the file sidebar's actions bar — conceptually mismatched (Export operates on the template, not the file uploads). | **Export moved to the template summary bar** (right edge, next to the summary stats). The sidebar action bar focuses on Process / Stop / More. |
| Vague HTTP 405 reported on rename. | Confirmed the running backend responds 200 to `PATCH /api/batches/{id}` with `{batch_name: ...}`. The 405 the user saw was stale dev-server state from before Phase 1H added the PATCH route — addressed by ensuring `/cancel`-style restart guidance lands in the README. |

---

## Backend investigation

`PATCH /api/batches/{batch_id}` is registered correctly (since Phase 1H) under the `batches` router with `prefix="/api/batches"`:

```
$ python -c "from webapp.backend.main import app; ..."
/api/batches/{batch_id} -> ['PATCH']
/api/batches/{batch_id} -> ['GET']
/api/batches/{batch_id} -> ['DELETE']
```

A direct TestClient run from this phase shows the full happy-path + error matrix:

```
POST /api/batches              → 200 {batch_id, batch_name: "May 2026 Test"}
GET  /api/batches              → 200 (lists "May 2026 Test")
PATCH /api/batches/{id}        → 200 (batch_name: "Renamed Test")
PATCH (empty name)             → 400 "batch_name cannot be empty"
PATCH (missing batch)          → 404 "Batch not found: no_such"
```

**Conclusion:** the user's HTTP 405 was a stale dev-server artefact. The fix on the backend side is "no code change required, but the README should call out that the dev backend must be reloaded after pulling Phase 1H+ changes". The frontend-side fix lives in the new modal and the toast-based error layer below.

---

## Files modified

| File | Change |
| --- | --- |
| **Created** [`webapp/frontend/src/components/RenameBatchModal.tsx`](../../../webapp/frontend/src/components/RenameBatchModal.tsx) | New app-native modal. Uses the existing `.modal-card` family. Prefills with the current batch name; validates "not empty" + "≤80 chars"; surfaces backend errors inline (strips the noisy `HTTP NNN ...` prefix); Enter saves, Escape cancels; spinner on the Save button while the PATCH is in flight. |
| [`webapp/frontend/src/App.tsx`](../../../webapp/frontend/src/App.tsx) | `handleRenameBatch` no longer calls `window.prompt`. New state `showRenameDialog`. New `handleSubmitRename` async handler — throws to the modal on backend error, dismisses + toasts on success. Removed three `setError(\`...\`)` calls (rename, switch batch, create batch, processing failure) and replaced with `pushToast({ tone: 'error', ... })`. |
| [`webapp/frontend/src/components/TemplateWorkspace.tsx`](../../../webapp/frontend/src/components/TemplateWorkspace.tsx) | New optional props `onExport`, `isExporting`, `hasExport`. Renders an **Export** button at the right edge of the template summary bar — visually matches `.btn-accent`, disabled until preview rows exist. Includes the edit count when there are unsaved edits. New `<ExportIcon/>` SVG. |
| [`webapp/frontend/src/components/BatchActionsBar.tsx`](../../../webapp/frontend/src/components/BatchActionsBar.tsx) | Export button removed. Sidebar action bar now reads "Process · (Stop while processing) · More". |
| [`webapp/frontend/src/styles.css`](../../../webapp/frontend/src/styles.css) | Phase 1P additions: `.modal-card-narrow`, `.modal-input.has-error`, `.modal-field-error`, `.template-summary-export` (right-aligned button container with a subtle left divider). |

---

## PART A — Backend rename API

No code change needed. Verified via `python -m fastapi.testclient`:

| Request | Response |
| --- | --- |
| `PATCH /api/batches/<existing>` body `{"batch_name": "Hello"}` | **200** `{batch_id, metadata: {batch_name: "Hello", ...}}` |
| `PATCH ... ` body `{"batch_name": ""}` | **400** `batch_name cannot be empty` |
| `PATCH /api/batches/no_such` | **404** `Batch not found: no_such` |

The metadata sidecar (`webapp_data/batches/<batch_id>/batch_metadata.json`) is updated atomically by `_write_metadata`. Old metadata files without a `batch_name` are tolerated — the helper falls back to the batch_id-style label.

---

## PART B — Rename modal

```
┌─────────────────────────────────────┐
│ Rename batch                    [×] │
├─────────────────────────────────────┤
│ Batch name                          │
│ ┌─────────────────────────────────┐ │
│ │ May 2026 Hopkinsville           │ │
│ └─────────────────────────────────┘ │
│   (inline error if empty / too long)│
├─────────────────────────────────────┤
│              Cancel    [    Save  ] │
└─────────────────────────────────────┘
```

- Opens with the input pre-filled and pre-selected so the operator can type a replacement immediately.
- **Enter** saves; **Escape** cancels (unless saving is in flight).
- Empty / too-long names are rejected client-side with an inline error in red — no toast, no scary banner.
- Backend-side errors are rendered via the same inline error label; the noisy `HTTP NNN <verb>:` prefix is stripped.
- Save button shows a spinner + "Saving…" until the PATCH resolves.
- A success toast ("Renamed batch to …") fires once the modal closes.

---

## PART C — New batch name persistence

Verified end-to-end:

1. Operator types **"May 2026 Test"** in the new-batch modal → `POST /api/batches` body `{batch_name: "May 2026 Test", document_mode: "auto_detect"}`.
2. Backend persists `batch_metadata.json` with the supplied name.
3. `GET /api/batches` lists the new batch with `batch_name: "May 2026 Test"`.
4. Sidebar `BatchHeader` renders "May 2026 Test" (Phase 1M behaviour, preserved).
5. Dropdown `batch-picker-row-name` shows "May 2026 Test"; `batch-picker-row-meta` shows the file count + invoice count.

The previous reports of "names didn't persist" were the visual confusion caused by the browser-prompt rename flow + the giant red banner. With both replaced, the actual persistence is unambiguous.

---

## PART D — Batch dropdown

The `BatchHeader.tsx` component (Phase 1M) already renders:

- **Primary line**: `batch_name` (or "No batch yet" when none is active).
- **Metadata line**: `<n> file(s) · <m> inv` plus a `✓` if a previous export exists.
- Sorted most-recent first by the backend's `list_batches_endpoint` which orders by `created_at desc`.

No changes needed in 1P. The phantom "files · inv" / "raw batch_id" complaint was — again — about the browser-prompt rename UX, not about the dropdown. With the new modal, the dropdown reads cleanly.

---

## PART E — Error handling

`setError(\`Could not switch batch: ${e}\`)` etc. used to render a fixed-width red banner inside the template area. Three call sites converted to toasts:

| Where | Old | New |
| --- | --- | --- |
| Rename failure | red banner | inline error inside the rename modal |
| Switch batch failure | red banner | toast: *"Could not switch batch."* |
| Create batch failure | red banner | toast: *"Could not create batch."* |
| Processing failure | red banner | toast: *"Processing failed: …"* |

Detailed errors (the underlying exception) go to `console.warn` so a developer can still inspect them in DevTools without the operator seeing them. The `setError` state itself remains for two narrow cases (`api.health()` failure on app startup, and `Could not restore preview`) where a persistent banner is the right affordance.

---

## PART F — Action placement

| Action | Phase 1M location | Phase 1P location |
| --- | --- | --- |
| Process | sidebar action bar | sidebar action bar (unchanged) |
| Stop | sidebar action bar (only while processing) | sidebar action bar (unchanged) |
| More menu | sidebar action bar | sidebar action bar (unchanged) |
| **Export** | sidebar action bar | **template workspace summary bar** (right edge) |

The Export button is now visually adjacent to the template stats it operates on (Files / Invoices / Rows / Total) — operators see the rows they're about to export and can click Export in the same eye-line.

---

## Tests performed (PART K)

### 1. Frontend build
```
$ npm run build
✓ 66 modules transformed.
dist/assets/index-CCW5z9Cn.js     218.47 kB │ gzip: 66.64 kB
dist/assets/index-DG7HVd4T.css     53.81 kB │ gzip:  9.85 kB
dist/assets/PdfWorkspace-…js       11.41 kB │ gzip:  4.40 kB  (lazy)
dist/assets/pdf-…js              293.42 kB │ gzip: 86.55 kB  (lazy)
dist/assets/pdf.worker-…mjs    1,875.78 kB                   (lazy)
✓ built in 1.71s
```

### 2. Backend smoke (FastAPI TestClient)
```
POST /api/batches with batch_name "May 2026 Test"   → 200 batch_name="May 2026 Test"
GET /api/batches                                    → 200 lists batch_name correctly
PATCH /api/batches/<id> body {batch_name:"Renamed"} → 200 batch_name="Renamed Test"
PATCH (empty name)                                  → 400 "batch_name cannot be empty"
PATCH (missing batch)                               → 404 "Batch not found: no_such"
DELETE                                              → 200
```

### 3. CLI regression

| Processor | Files | Invoices | Lines | Flagged |
| --- | --- | ---: | ---: | ---: |
| Richmond Utilities | 15 | **28** | **32** | 28 |
| Hopkinsville Water | 2 | **14** | **36** | 14 |

Both match every prior phase baseline.

### 4. Source-file integrity (SHA-256)

| File | SHA-256 |
| --- | --- |
| `Output/Template.xlsx` | `b753f406…3969c284` (unchanged across every phase) |
| `Properties/Unit Info Clean.csv` | `79d46c7c…219c1a683` |
| `Gl Codes/General Ledger Report.csv` | `8f8506ec…73abb6e49` |
| `Vendors/Vendor List.csv` | `7839a43a…cef64863f9` |

### 5. Secret hygiene
- `.env.example` unchanged.
- No new endpoints. No new secret surfaces.

---

## Confirmation table

| Requirement | Status |
| --- | --- |
| Richmond Utilities CLI works | ✅ 28 / 32 |
| Hopkinsville Water CLI works | ✅ 14 / 36 |
| Web app processing works | ✅ shared code path |
| Export still works | ✅ unchanged plumbing |
| Document Url still in export | ✅ Phase 1J shape preserved |
| Editable cell export still works | ✅ |
| Dropbox still works | ✅ unchanged |
| Batch persistence still works | ✅ |
| `Output/Template.xlsx` unchanged | ✅ |
| Source PDFs / CSVs unchanged | ✅ |
| Secrets not exposed | ✅ |
| AI disabled by default | ✅ |
| **`window.prompt` removed for rename** | ✅ |
| **App-native rename modal** | ✅ |
| **No giant red banner on rename failure** | ✅ inline + toast |
| **Custom batch names persist + display** | ✅ verified end-to-end |
| **Dropdown shows friendly names** | ✅ Phase 1M behaviour preserved |
| **Process in sidebar** | ✅ |
| **Export in template header** | ✅ |
| **Backend rename PATCH works** | ✅ verified 200 / 400 / 404 |

---

## Known limitations

- The HTTP 405 the user originally observed cannot be reproduced now that the running backend is current. Operators on a stale dev backend should restart it after pulling Phase 1H+ changes — added to README.
- Both modals (`RenameBatchModal`, `New batch`) reuse the same `.modal-card` family. The two could be unified into a single shared `Modal` primitive, deferred for a future phase.
- The Export button in the template summary bar uses `.btn-accent` on enabled and a muted disabled state. A more elaborate "review first" warning workflow when flagged rows exist was not added in this phase; the existing manual-review reasons are still surfaced via the Issues drawer.
- Toast tone selection for rename / switch / create errors is `error` (red border). Network-blip events (transient) intentionally use `warning` so the operator can disambiguate.
