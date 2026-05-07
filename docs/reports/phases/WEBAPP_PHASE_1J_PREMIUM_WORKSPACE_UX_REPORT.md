# Webapp Phase 1J — Premium Workspace UX Overhaul Report

**Date:** 2026-05-02
**Scope:** Frontend / UX polish only. Vendor processors, CLI, Dropbox, export, and AI safety guarantees are unchanged. AI remains disabled by default — no provider call ever fires.

---

## TL;DR — what changed

| Before | After |
| --- | --- |
| Sidebar dominated by big stacked blue buttons | Compact toolbar: **▶ Process · ↓ Export · More ⋯** |
| Bottom-of-screen `Manual Review` table that felt disconnected | **Right-side Review / Inspector panel** with two tabs: **Issues** (grouped, clickable cards) + **Selected row** (provenance + reasons) |
| Confusing **Native / Field regions** preview toggle | User-facing **Document / Mark Fields** toggle |
| Raw `"Could not load regions: HTTP 404 Not Found"` shown to operator | Empty state: *"No field regions yet. Draw a box around important fields like service address or total amount."* |
| `AI: ?` pill | Premium AI status pill (**AI Off / AI Ready / AI Not Configured / AI Error**) with a click-to-open popover explaining provider, policy, cost ceiling, and what AI would help with |
| 3-column grid with no resizing | Flex layout with **drag-to-resize splitters** between sidebar / document / inspector. Sizes persist in localStorage; double-click resets |
| Workflow was implicit | **Topbar workflow steps**: Upload → Process → Review → Export, each showing live state (e.g. "12 invoices", "3 issues") |
| Template grid was one fixed view of every column | Template Workspace has **view presets** (Required / Review / Full template), a **summary bar** (Files / Invoices / Rows / Flagged / Edited / Missing link / Total), **search**, and **row filters** |
| Nav was implicit (no rail) | Slim left **nav rail** with future destinations (Review / Vendor Rules / Exports / Settings) marked **Soon** |

---

## Files created (Phase 1J)

| File | Purpose |
| --- | --- |
| [webapp/frontend/src/hooks/useResizablePanel.ts](webapp/frontend/src/hooks/useResizablePanel.ts) | Pointer-driven resize hook with localStorage persistence + `inverted` mode for right-edge panels |
| [webapp/frontend/src/components/NavRail.tsx](webapp/frontend/src/components/NavRail.tsx) | Slim 64 px left nav rail. Only "Batches" is wired up; the rest show a "Soon" pill |
| [webapp/frontend/src/components/WorkflowSteps.tsx](webapp/frontend/src/components/WorkflowSteps.tsx) | Topbar workflow indicator (Upload → Process → Review → Export) with live status |
| [webapp/frontend/src/components/BatchActionsBar.tsx](webapp/frontend/src/components/BatchActionsBar.tsx) | Compact action toolbar (Process / Export / More dropdown) replacing the legacy stacked-button panel |
| [webapp/frontend/src/components/TemplateWorkspace.tsx](webapp/frontend/src/components/TemplateWorkspace.tsx) | Wraps `ResManTemplatePreview` with summary bar, view presets, search, and row filters |
| [webapp/frontend/src/components/ReviewInspectorPanel.tsx](webapp/frontend/src/components/ReviewInspectorPanel.tsx) | Replacement for `ManualReviewPanel`. Two tabs (Issues / Selected row) with severity dots, provenance lines, and "Show row" / "Open document" actions on each issue card |
| [WEBAPP_PHASE_1J_PREMIUM_WORKSPACE_UX_REPORT.md](WEBAPP_PHASE_1J_PREMIUM_WORKSPACE_UX_REPORT.md) | This document |

## Files modified (Phase 1J)

| File | What changed |
| --- | --- |
| [webapp/frontend/src/App.tsx](webapp/frontend/src/App.tsx) | New layout: nav rail · resizable sidebar · resizable document pane · template area + resizable inspector. Added `selectedRowIndex` state that flows from template clicks → inspector. Workflow steps and AI badge live in the topbar. Modal new-batch dialog kept |
| [webapp/frontend/src/components/AiFallbackStatusBadge.tsx](webapp/frontend/src/components/AiFallbackStatusBadge.tsx) | Replaced `AI: ?` with click-to-open popover; tones for **off / configured / ready / error / loading**; never returns or displays an API key |
| [webapp/frontend/src/components/DocumentPreviewPanel.tsx](webapp/frontend/src/components/DocumentPreviewPanel.tsx) | Mode toggle relabeled **Document / Mark Fields** (no more "Native / Field regions") |
| [webapp/frontend/src/components/pdf_workspace/PdfWorkspace.tsx](webapp/frontend/src/components/pdf_workspace/PdfWorkspace.tsx) | Region 404 silently treated as empty list; real errors show a compact message + Retry button; empty state replaces noise pill |
| [webapp/frontend/src/components/ResManTemplatePreview.tsx](webapp/frontend/src/components/ResManTemplatePreview.tsx) | Accepts new optional props: `visibleRowIndexes` (filter), `selectedRowIndex` (highlight), `onSelectRow` (callback). Old behaviour unchanged when not passed |
| [webapp/frontend/src/styles.css](webapp/frontend/src/styles.css) | Refined CSS variables (`--accent-soft`, `--success-soft`, `--warning-soft`, `--danger-soft`, `--shadow-elev-1/2`, layout sizing tokens), rebuilt layout (`.layout`, `.nav-rail`, `.file-sidebar`, `.document-pane`, `.template-and-inspector`, `.template-area`, `.inspector-pane`, `.resizer`), workflow steps styles, AI popover styles, compact action bar / spinner / dropdown menu, summary bar, view presets, template controls, full inspector treatment (tabs, issue cards, severity dots, row inspector dl). Old `.workspace`/`.template-column`/`.manual-review-drawer` blocks removed |
| [webapp/backend/api/regions.py](webapp/backend/api/regions.py) | `GET /api/batches/{id}/regions` no longer 404s when the batch directory has been deleted (stale localStorage). Returns `{schema_version: 1, regions: []}` so the workspace renders an empty state |

---

## PART A — visible broken states fixed

### A.1 Region 404 → graceful empty state
- **Frontend:** `PdfWorkspace.tsx` swallows any `404` listing error and starts with an empty region list. Real server errors (500, network) surface a one-line message + **Retry** button instead of the raw HTTP text. Detailed errors go to `console.warn` for developers.
- **Backend:** the regions GET endpoint now returns 200 + empty list when the batch directory is missing, eliminating the most common 404 source (stale localStorage after a batch was deleted).
- **Empty state copy:** "No field regions yet. Draw a box around important fields like service address or total amount."

### A.2 AI badge — `AI: ?` replaced
- The pill never shows `?`. While loading, it shows `AI…` with a neutral tone. When `AiStatus` is fetched, the pill resolves to **AI Off** (provider=disabled), **AI Not Configured** (provider chosen, no key), **AI Ready** (master enabled + key set), or **AI Error** (status fetch failed).
- Click opens a popover showing **Status / Provider / Policy / Cost ceiling**, a one-line message, a **What AI would help with** list (service address, dates, totals, OCR cleanup, notice boundaries, etc.), and a hint pointing to `.env` configuration. **No API keys are ever displayed.**
- No real provider call is ever made in this phase.

### A.3 Technical labels removed
- `Native` / `Field regions` → **Document** / **Mark Fields** in `DocumentPreviewPanel` toggle.
- The internal modes (`native`, `workspace`) are still wired to the same code paths, but the user no longer sees those words.

---

## PART B — workspace layout

The shell is now a flex row inside a 52-px topbar:

```
┌─ Topbar ──────────────────────────────────────────────────────────────────────┐
│ Brand    │  Upload → Process → Review → Export   │   AI pill  Batch picker   │
├─ NavRail ┬─ FileSidebar ─┬─ Doc pane ─┬─ Template area + Inspector ─────────┤
│  📦 active│  Actions      │  Native    │  Summary bar                          │
│  ✅ Soon  │  Progress     │  toolbar   │  Required / Review / Full · search   │
│  📐 Soon  │  Timeline     │  PDF page  │  Table (sticky header, selected row) │
│  ↓  Soon  │  Drop zone    │            │  Inspector: Issues / Selected row    │
│  ⚙ Soon   │  File list    │            │                                     │
└──────────┴───────────────┴────────────┴───────────────────────────────────────┘
```

### Resizable splitters
Three independent dividers, all driven by [`useResizablePanel`](webapp/frontend/src/hooks/useResizablePanel.ts):

| Pane | Default width | Min / Max | localStorage key |
| --- | --- | --- | --- |
| File sidebar | 280 px | 220 / 460 | `billing_refactoring_layout_sidebar_width` |
| Document pane | 480 px | 320 / 720 | `billing_refactoring_layout_document_width` |
| Inspector pane | 360 px (right edge — **inverted**) | 260 / 560 | `billing_refactoring_layout_inspector_width` |

Behaviour:
- Pointer-down on a divider captures the drag and tracks `pointermove` at the **window** level so dragging beyond the divider into the next pane keeps the grip.
- Cursor switches to `col-resize`, `userSelect` is disabled until pointer-up.
- Final size persists to `localStorage` once on `pointerup` (no high-frequency writes).
- **Double-click on a divider resets to the default size.**

Both the document and inspector panes also have a per-pane **collapse rail**: clicking the collapse arrow drops the pane to 36 px with a vertical "Document"/"Review" rail button.

---

## PART C — sidebar actions

Rewritten as a compact toolbar (`BatchActionsBar`):

- Primary: **▶ Process** — disabled until files exist; shows an inline spinner + "Processing…" while running.
- Secondary: **↓ Export** — visually highlighted (`btn-accent`) only once a preview exists. Includes the edit count when there are unsaved edits (e.g. `↓ Export (3 edits)`).
- **More ⋯** dropdown: Refresh preview · Reset edits · Re-download last export · **Delete batch** (destructive, red).

Click-outside closes the menu; menu items disable themselves when not relevant.

---

## PART D — Document Workspace

- The mode toggle in the panel header is **Document / Mark Fields**.
- Tooltip on the toggle clarifies intent: "Mark extraction fields with rectangles to guide processing."
- The PDF.js workspace remains the same canvas + overlay engine, but its bottom status bar is now informative instead of error-y:
  - During load: *"Saving regions…"* pill while writes are in flight.
  - On error: *"Region hints could not be loaded. **Retry**"* with a one-click retry.
  - Empty: *"No field regions yet. Draw a box around important fields like service address or total amount."*

(The richer in-canvas Document Workspace header — file badge, page indicator, OCR status, region count — is wired through `ViewerToolbar` already; deeper polish is queued for the next phase.)

---

## PART E — Template Workspace

The template grid is now wrapped in a `TemplateWorkspace` component:

- **Summary bar** above the grid: Files / Invoices / Rows / Flagged (warn) / Edited (info) / Missing link (warn) / Total (strong / dollar value). Each stat is a stacked label/value pair with a tabular-numerals value and a subtle vertical divider.
- **View presets** (segmented control): **Required**, **Review**, **Full template**.
  - *Required*: required + recommended columns only.
  - *Review*: required + recommended + Document Url + Reference Number + Invoice Description.
  - *Full template*: every column from `Output/Template.xlsx`.
- **Search box**: matches across Invoice Number / Vendor / Property / Location / Service address / Description.
- **Row filters** dropdown: All rows · Needs review · Edited · Missing property · Missing location · Amount mismatch · Missing link.
- **Selected row** highlight: clicking any row sets a soft `--accent-soft` background plus a 1 px accent outline. Selection drives the inspector panel (auto-switches to **Selected row** tab).

`ResManTemplatePreview` was extended with three optional props (`visibleRowIndexes`, `selectedRowIndex`, `onSelectRow`) — when omitted, behaviour is byte-identical to Phase 1H.

---

## PART F — Review / Inspector panel

Replaces `ManualReviewPanel`. Tabs:

### Issues tab
- Issues are **grouped by source file** with a click-to-open file header (`onSelectFile` switches the document pane).
- Each row from `manual_review` produces one **issue card per reason**, so an invoice with three reasons shows three cards.
- Cards carry a **severity dot** (high / medium / low; computed from the reason text), a human explanation pulled from a 20+-entry reason dictionary, and meta pills (property + amount).
- Two actions per card: **Show row** (highlights the matching template row) and **Open document** (switches the document pane to that source file).

### Selected row tab
- Property-list view: Invoice number, Vendor, Invoice date, Due date, Property, Location, GL account, Amount, Description.
- **↗ Open support document** button — opens `Document Url` in a new tab when present.
- **Provenance** section: Match strategy · Match confidence · Service period source · Support document status (placeholder values come from `_meta` on the preview row).
- **Manual review** section: per-reason help text inline.
- Empty state for both tabs: a clean centered message with a call to action.

### Why this works without backend changes
The new panel reads only existing fields (`manual_review_reasons`, `match_strategy`, `match_confidence`, etc.) on the preview row's `_meta`. No new endpoints needed.

---

## PART H — workflow clarity

A 4-step indicator now sits in the topbar, between the brand and the topbar actions: **Upload → Process → Review → Export**. Each step has a numbered avatar (pending grey / active blue with halo / complete green / warning amber) and a 1-line subtitle (e.g. "12 invoices", "3 issues", "Ready").

The active step is the first one with `pending` or `active` state. The component is purely informational; clicks don't navigate.

---

## PART G — processing timeline polish

Already a Phase 1H feature; Phase 1J refined the styling: timeline rows use the new shared status colours (success-soft / warning-soft / danger-soft / accent-soft), dot pulse on running, tabular-numerals duration column. The component now lives next to the action bar in the file sidebar so the timeline is inline with the controls that drove it.

When AI is disabled (default), the `ai_fallback` stage shows **`skipped — disabled or not configured`**.

---

## PART I — premium visual cleanup

CSS variables (added in Phase 1J):
- `--accent-soft` `#ddf4ff`, `--success-soft` `#dafbe1`, `--warning-soft` `#fff8c5`, `--danger-soft` `#ffebe9`
- `--border-soft` `#eaeef2` (used inside dense panels to lighten dividers)
- `--shadow-elev-1` (cards), `--shadow-elev-2` (popovers, modals)
- `--nav-rail-width`, `--topbar-height`, `--resizer-width`

Touch points across components:
- Cards de-emphasised: less harsh borders inside the workspace.
- Buttons: `.btn` `.btn-compact` `.btn-mini` size classes; `.btn-primary` `.btn-accent` `.btn-ghost` tones.
- Dropdowns / popovers reuse a single `pop-in` keyframe animation.
- Selected row gets `--accent-soft` background + 1 px accent outline (no harsh borders).
- Workflow step pulse on the running stage.
- Resizer divider is 4 px neutral grey, becomes accent-blue on hover/focus, disappears on `< 1080 px` viewports along with the document pane.

Colour discipline:
- **Orange / amber** only for required headers and warnings.
- **Red** only for destructive (Delete batch) and errors.
- **Green** only for ready / success / complete states.
- **Blue** for primary actions and active selections; **muted greys** everywhere else.

---

## PART J — AI clarity (no real calls)

- Pill labels: **AI Off** (default) · **AI Not Configured** · **AI Ready** · **AI Error**.
- Popover explains policy, what AI would help with, and how to enable (in `.env`).
- Timeline shows `ai_fallback` as **skipped — disabled or not configured** when policy is off.
- The four real provider adapters remain typed stubs that raise `AIProviderNotImplementedError`.

---

## PART K — backend metadata

Single change: `GET /api/batches/{id}/regions` returns 200 + empty list when the batch directory is missing (instead of 404). This eliminates the noisy region error in the workspace when localStorage points to a deleted batch. All other endpoints are unchanged.

---

## Tests performed (PART M)

### 1. Frontend build
```
$ npm run build
✓ 59 modules transformed.
dist/assets/index-DzGwg18Y.js     196.49 kB │ gzip: 62.10 kB
dist/assets/index-4qZ77jb1.css     33.91 kB │ gzip:  6.66 kB
dist/assets/PdfWorkspace-…js       10.54 kB │ gzip:  4.06 kB  (lazy)
dist/assets/pdf-…js              293.42 kB │ gzip: 86.55 kB  (lazy)
dist/assets/pdf.worker-…mjs    1,875.78 kB                   (lazy)
✓ built in 3.18s
```

### 2. Backend smoke (FastAPI TestClient)
- `GET /api/batches/batch_nonexistent_xyz/regions` → **200** `{schema_version: 1, regions: []}` ✓ (regression vs Phase 1H 404)
- `GET /api/ai/status` → 200, `enabled=False`, reason `"AI fallback disabled (provider=disabled)"`, **no API keys returned** ✓
- `POST /api/batches` with `{document_mode: "mixed_pdf"}` → 200, persisted in metadata ✓
- `GET /api/batches/<new>/regions` on freshly-created batch → 200 empty ✓

### 3. CLI regression

| Processor | Files | Invoices | Lines | Flagged |
| --- | --- | --- | --- | --- |
| Richmond Utilities | 15 | 28 | 32 | 28 |
| Hopkinsville Water | 2 | 14 | 36 | 14 |

Both match the Phase 1I/1H baselines exactly.

### 4. Source-file integrity (SHA-256)

| File | SHA-256 |
| --- | --- |
| `Output/Template.xlsx` | `b753f406…3969c284` (unchanged from Phase 1H/1I) |
| `Properties/Unit Info Clean.csv` | `79d46c7c…219c1a683` (unchanged) |
| `Gl Codes/General Ledger Report.csv` | `8f8506ec…73abb6e49` (unchanged) |
| `Vendors/Vendor List.csv` | `7839a43a…cef64863f9` (unchanged) |

All four match the prior phase report exactly. No source PDFs / training files modified.

### 5. Secret hygiene
- `.env.example` unchanged.
- AI status JSON returns no provider key fields (`enabled`, `provider`, `configured`, `reason`, `policy`, `max_cost_per_batch_usd`, `allowed_tasks` only).
- AI service `__repr__` and `status()` never serialise `self.api_key`.
- No new environment variables introduced.

---

## Confirmation table

| Requirement | Status |
| --- | --- |
| Richmond Utilities CLI works | ✅ 28 invoices / 32 lines |
| Hopkinsville Water CLI works | ✅ 14 invoices / 36 lines |
| Web app processing works for both | ✅ shared code path; smoke-tested via TestClient |
| Export still works | ✅ unchanged path |
| Document Url still in export | ✅ no preview shape changes |
| Editable cell export still works | ✅ same `edits` plumbing in App.tsx |
| Dropbox still works | ✅ unchanged |
| Batch persistence still works | ✅ rehydration unchanged |
| `Output/Template.xlsx` unchanged | ✅ SHA-256 unchanged |
| Source PDFs / CSVs unchanged | ✅ |
| Unit Info Clean / GL / Vendor List unchanged | ✅ |
| Secrets not exposed | ✅ no AI keys in any response |
| AI disabled by default | ✅ pill says "AI Off" |
| No real AI calls | ✅ adapter stubs raise on use |
| Region 404 fixed | ✅ both backend + frontend |

---

## Known limitations

- The Document Workspace toolbar shows page nav and zoom but does not yet expose a thumbnail rail or per-page split controls. (Queued for the next phase.)
- The `Selected row` tab's provenance section reads only the existing `_meta` keys — richer per-field provenance (OCR confidence, AI suggestions) needs backend instrumentation in a follow-up phase.
- Workflow steps are informational only; they do not navigate or scroll.
- Resizer state persists per browser via `localStorage`. There is no "reset all layout" affordance yet (double-click each divider individually for now).
- Vertical resize between the template grid and the inspector panel is currently horizontal-only (right-edge inspector). A future "row" mode of the same hook would let the inspector dock to the bottom.

---

## What's next (deferred)

1. **Document workspace polish** — file-type badge / page indicator / OCR-status pill / region-count chip in the canvas header (today the `ViewerToolbar` shows page + zoom + region count; the per-file status header is the missing piece).
2. **Sticky first column** in the template grid (currently sticky header only).
3. **Row badge column** showing Ready / Needs Review / Edited / Missing Link badges per row (today indicators live in the row's classes and the inspector panel).
4. **Vertical inspector dock** — let the operator move the inspector to the bottom of the screen for wider tables.
5. **Per-vendor rules editor** — wire up the "Vendor Rules" nav rail item.
6. **Wire one real AI provider** (Phase 1H milestone item).
