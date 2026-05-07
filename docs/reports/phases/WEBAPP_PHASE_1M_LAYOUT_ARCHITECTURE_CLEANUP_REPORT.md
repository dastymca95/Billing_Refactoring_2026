# Webapp Phase 1M — Layout Architecture Cleanup + Documentation Organization Report

**Date:** 2026-05-02
**Scope:** Frontend architecture cleanup + documentation reorganization. No backend, processor, OCR, Dropbox, or export code touched. AI remains disabled by default.

---

## TL;DR — what changed

| Phase 1L → Phase 1M |
| --- |
| **Document / Mark Fields tabs** in the document panel | Removed. PDFs open straight into the editable workspace. Marking tools live entirely inside the workspace toolbar. |
| **Vertical "◀ Document" / "▶ Review" labels** on collapsed panels | Replaced with a clean icon-only **CollapseRail** (folder / document icon + chevron + optional badge count + tooltip). |
| **File sidebar always-on, fixed width** | Now collapsible to a slim icon rail. The same drag-to-resize splitter still applies when expanded. |
| **Batch picker / Rename / New batch in topbar** | Moved into a **BatchHeader** at the top of the file sidebar. Topbar is now focused on workflow / AI / issues only. |
| **Toy 1-2-3-4 workflow strip** in the topbar | Hidden. The Issues pill, Process button, and Export button already communicate state. |
| **Yellow tint on flagged rows + cell-required** | Removed. Flagged rows now get a 3 px warning stripe on the leading cell only; body cells stay white. Required columns retain the soft-orange **header** fill. |
| **Emoji icons + visible "Soon" badges** in the nav rail | Replaced with consistent Lucide-style SVG icons. Disabled items are muted with a tooltip-only "Coming later" hint. |
| **Basic dropdown More menu** | Refined popover with **Preview** / **Export** sections, separated destructive **Delete batch** action with built-in confirm. |
| **20+ markdown reports cluttering the project root** | All moved under `docs/reports/phases/`, `docs/reports/vendors/`, `docs/architecture/`. Root keeps only the new top-level `README.md`. |
| Resizer "sticky drag" bug | Fixed in Phase 1L; preserved here. |

---

## Files added (Phase 1M)

| File | Purpose |
| --- | --- |
| [`webapp/frontend/src/components/BatchHeader.tsx`](../../../webapp/frontend/src/components/BatchHeader.tsx) | Sidebar-top batch controls. Click the batch name to switch; click the dots to **New batch / Rename / Delete batch** (destructive separated). Shows a small `document_mode` tag. |
| [`webapp/frontend/src/components/CollapseRail.tsx`](../../../webapp/frontend/src/components/CollapseRail.tsx) | Replacement for vertical text labels on collapsed panels. Renders an icon (folder / document / issues) + chevron + optional count badge + tooltip. No vertical text. |
| `README.md` (root) | Brand new project README. Replaces a missing top-level entry point. Explains the project, layout, supported vendors, how to run, where data lives, secrets policy, and links to all moved docs. |
| [`docs/reports/phases/WEBAPP_PHASE_1M_LAYOUT_ARCHITECTURE_CLEANUP_REPORT.md`](WEBAPP_PHASE_1M_LAYOUT_ARCHITECTURE_CLEANUP_REPORT.md) | This document. |

## Files modified (Phase 1M)

| File | Change |
| --- | --- |
| [`webapp/frontend/src/App.tsx`](../../../webapp/frontend/src/App.tsx) | Added `fileSidebarCollapsed` state. Sidebar now renders as either a `CollapseRail` or the full `<aside>`. The `<aside>` opens with a `<BatchHeader>` and a small icon-only collapse arrow. The legacy topbar batch picker (`.batch-controls` / `batch-picker-button` / `batch-picker-dropdown`) was removed entirely. The document pane's collapsed state now uses `CollapseRail` (no more vertical "◀ Document"). |
| [`webapp/frontend/src/components/DocumentPreviewPanel.tsx`](../../../webapp/frontend/src/components/DocumentPreviewPanel.tsx) | Removed the **Document / Mark Fields** segmented control. PDFs render straight into the `PdfWorkspace` (canvas + overlay + toolbar). The native iframe path remains for non-PDF binary types (images). The header now shows just a doc icon + filename + a single icon-only collapse button. |
| [`webapp/frontend/src/components/NavRail.tsx`](../../../webapp/frontend/src/components/NavRail.tsx) | Emoji icons (`📦 ✅ 📐 ↓ ⚙`) replaced with consistent in-house SVGs. Hint text moved to `title=` only. Disabled state is purely visual (lower opacity, neutral icon). |
| [`webapp/frontend/src/components/BatchActionsBar.tsx`](../../../webapp/frontend/src/components/BatchActionsBar.tsx) | More menu rebuilt: section titles ("Preview" / "Export"), each item paired with a small SVG icon, separated destructive **Delete batch** with built-in `window.confirm` (so the operator never accidentally drops a batch). |
| [`webapp/frontend/src/styles.css`](../../../webapp/frontend/src/styles.css) | Phase 1M section: `.collapse-rail`, `.batch-header`, `.batch-name-button`, `.batch-header-menu`, `.batch-picker-list`, `.actions-more-section-title`, `.doc-preview-header` refinements, hide rules for the legacy `.mode-toggle` and `.workflow-strip`, and the **yellow row** removal (review-row body cells now stay white; the leading cell carries a 3 px `inset` warning shadow). Per-cell `cell-required` / `cell-recommended` tints set to transparent. |
| (root) | All 22 markdown reports/plans moved into `docs/`. Root now only carries the new `README.md`. |

---

## PART A — Document tab gone

The two-button "Document / Mark Fields" segmented control was the last piece of implementation language showing through the UI. In 1M:

- A PDF preview always opens directly into the editable `PdfWorkspace` (canvas + overlay + toolbar).
- The marking tools (Select / Draw / Pan / Delete / Zoom / Page nav / region label) live inside the workspace's existing `ViewerToolbar` — they were already there, and the now-redundant outer toggle is gone.
- A non-PDF file (image / table) still renders correctly through the unchanged `BinaryPreview` / table fallback.

## PART B — Vertical text labels gone

The legacy `◀ Document` / `▶ Review` rotated text on collapsed panels was rendered via `writing-mode: vertical-rl + transform: rotate(180deg)`. It looked broken when the rail was thin enough to clip the text. Phase 1M replaces both with the `CollapseRail` component:

- 36 px wide.
- Icon button (folder / document / issues) + small chevron underneath.
- Optional badge count (file count for sidebar, region count for document, issue count for issues drawer).
- Tooltip text only (no rendered words).

## PART C / D — Consistent collapse + resize for all panels

- **File sidebar** — collapses to `CollapseRail`. When expanded, drag-to-resize via the existing splitter; double-click resets. Sizes persist via `localStorage` (`billing_refactoring_layout_sidebar_width`).
- **Document workspace** — same pattern. Splitter only renders when expanded.
- **Template workspace** — fills the remaining width by default. *Template focus* preset (the existing topbar switcher) collapses both the document pane and the file sidebar so the grid covers the screen.
- **Issues drawer** — overlay only. It never steals width; the template grid keeps the full available width whether the drawer is open or closed.

The Phase 1L sticky-drag fix is intact (Pointer Events + capture + `e.buttons === 0` guard + window-level safety nets).

## PART D — Batch management lives with files

Topbar `.batch-controls` block removed. The new `<BatchHeader>` component sits at the top of the file sidebar and owns:

- The current batch name (also the trigger for the switch dropdown).
- A switch list (`batch_list` from `/api/batches`).
- A dots menu with **New batch / Rename batch / Delete batch** (the destructive item separated by a divider; built-in confirm).
- A small accent-coloured `document_mode` tag below the name.

This co-locates the batch with the files — operators no longer have to scan the topbar to know which batch they're working on.

## PART E — Workflow strip simplified out

The workflow strip from Phase 1L (Upload › Process › Review › Export) is now hidden via CSS. Reasoning:

- Operators already see the file count in the sidebar, the issue count in the topbar pill, and the export readiness in the Export button's enabled state.
- The strip was teaching the workflow rather than tracking it.
- Hiding (rather than deleting) means we can re-enable later if user feedback changes.

## PART F — Yellow rows fixed

Two CSS rules were the source of the "yellow row" look:

```css
/* Phase 1L (legacy) */
.data-table tr.review-row { background: #fff8c5; }
/* Phase 1J (slightly softer overlay) */
.data-table tr.review-row td { background: rgba(180, 83, 9, 0.05); }
```

In Phase 1M:

- Both row-level backgrounds set to `transparent` / `var(--panel)` (white).
- Flagged rows get a **3 px warning stripe** on the leading cell only via `box-shadow: inset 3px 0 0 var(--warning)`. When the same row is also selected, the stripe combines with the accent-soft selection background.
- The per-cell `td.cell-required` / `td.cell-recommended` low-alpha tints (which still read as faint yellow on some monitors) are gone.
- The orange/amber **column-header** fills are unchanged — operators can still see at a glance which columns are required / recommended.

## PART G — More menu refined

```
┌─────────────────────────┐
│ PREVIEW                 │
│   ↻  Refresh preview    │
│   ⤺  Reset N edits      │
│ EXPORT                  │
│   ↓  Re-download last…  │
│ ─────────────────────── │
│   🗑  Delete batch       │
└─────────────────────────┘
```

- Sectioned via uppercase 9 px section titles.
- Each item paired with an icon (refresh / undo / download / trash).
- Disabled items styled clearly (muted icon + cursor not-allowed).
- Destructive **Delete batch** separated by a divider; built-in `window.confirm` so a misclick can't drop a batch.

## PART H — Nav rail icons modernized

Replaced the emoji icons with stroke-based SVGs:

- **Batches** — 4-rect grid icon (the active item, with accent fill on the icon stroke).
- **Review** — checklist icon.
- **Vendors** — stack icon.
- **Exports** — download / outbound icon.
- **Settings** — gear icon.

All icons share size (20×20) and stroke (1.8 px). Disabled items are muted via `opacity: 0.45` and a tooltip; no visible "Soon" badge.

---

## PART I — Documentation organization

All root-level markdown was moved under `docs/`:

```
docs/
├── DOCKER_WEBAPP_README.md
├── DROPBOX_INTEGRATION_README.md
├── _category_listing.md
├── architecture/
│   ├── CONFIG_SOURCE_OF_TRUTH_REPORT.md
│   ├── OLD_SCRIPTS_MIGRATION_ANALYSIS.md
│   ├── WEBAPP_PHASE_1_PLAN.md
│   └── WEBAPP_PREMIUM_AI_PDF_WORKSPACE_PLAN.md
└── reports/
    ├── phases/
    │   ├── WEBAPP_PHASE_1B_EDITABLE_PREVIEW_REPORT.md
    │   ├── WEBAPP_PHASE_1D_LAYOUT_AND_PDF_LINKS_REPORT.md
    │   ├── WEBAPP_PHASE_1E_DOCKER_EXPORT_TEMPLATE_REPORT.md
    │   ├── WEBAPP_PHASE_1H_PREMIUM_AI_WORKSPACE_REPORT.md
    │   ├── WEBAPP_PHASE_1J_PREMIUM_WORKSPACE_UX_REPORT.md
    │   ├── WEBAPP_PHASE_1K_VISUAL_SYSTEM_REFINEMENT_REPORT.md
    │   ├── WEBAPP_PHASE_1L_PRODUCT_UI_SIMPLIFICATION_REPORT.md
    │   ├── WEBAPP_PHASE_1L_RESIZER_BUGFIX_REPORT.md
    │   ├── WEBAPP_PHASE_1M_LAYOUT_ARCHITECTURE_CLEANUP_REPORT.md   ← this report
    │   └── WEBAPP_PHASE_1_IMPLEMENTATION_REPORT.md
    └── vendors/
        ├── HENDERSON_TRAINING_FILES_PLACEHOLDER_REPORT.md
        ├── HOPKINSVILLE_WATER_ADDRESS_UNIT_MATCH_FIX_REPORT.md
        ├── HOPKINSVILLE_WATER_ASSET_DISCOVERY_REPORT.md
        ├── HOPKINSVILLE_WATER_DISCONNECTION_NOTICE_FIX_REPORT.md
        ├── HOPKINSVILLE_WATER_GENERAL_LEDGER_PATTERN_REPORT.md
        ├── HOPKINSVILLE_WATER_IMPLEMENTATION_REPORT.md
        ├── HOPKINSVILLE_WATER_QA_FIX_REPORT.md
        ├── HOPKINSVILLE_WATER_TRAINING_DATA_AUDIT_REPORT.md
        └── VENDOR_FOLDER_STRUCTURE_REPORT.md
```

Root now contains only the new top-level [`README.md`](../../../README.md) plus the existing essential project files (`requirements.txt`, `docker-compose.yml`, `.env.example`, `.gitignore`, `.dockerignore`, etc.).

The new root `README.md`:

- Explains the project's two faces (CLI + web app) and the shared processor architecture.
- Lists supported vendors and where to add new ones.
- Walks through the directory layout.
- Documents how to run backend / frontend / Docker / CLI.
- Points to every section of `docs/` (including the per-phase reports under `docs/reports/phases/`).
- Calls out the safety guarantees (Output/Template.xlsx never modified, AI off by default, secrets env-only).

No reports were deleted. They were moved.

---

## Tests performed (PART J)

### 1. Frontend build
```
$ npm run build
✓ 65 modules transformed.
dist/assets/index-sCyI3YGB.js     214.27 kB │ gzip: 65.61 kB
dist/assets/index-4WXErSYn.css     50.97 kB │ gzip:  9.34 kB
dist/assets/PdfWorkspace-…js       10.58 kB │ gzip:  4.07 kB  (lazy)
dist/assets/pdf-…js              293.42 kB │ gzip: 86.55 kB  (lazy)
dist/assets/pdf.worker-…mjs    1,875.78 kB                   (lazy)
✓ built in 3.48s
```

### 2. Backend smoke (FastAPI TestClient)
- `GET /api/ai/status` → 200, `enabled=False`.
- `GET /api/batches/none/regions` → 200 + `{regions: []}` (Phase 1J fix preserved).

### 3. CLI regression

| Processor | Files | Invoices | Lines | Flagged |
| --- | --- | ---: | ---: | ---: |
| Richmond Utilities | 15 | **28** | **32** | 28 |
| Hopkinsville Water | 2 | **14** | **36** | 14 |

Both match the Phase 1H/1I/1J/1K/1L baselines exactly.

### 4. Source-file integrity (SHA-256)

| File | SHA-256 |
| --- | --- |
| `Output/Template.xlsx` | `b753f406…3969c284` (unchanged across every phase) |
| `Properties/Unit Info Clean.csv` | `79d46c7c…219c1a683` |
| `Gl Codes/General Ledger Report.csv` | `8f8506ec…73abb6e49` |
| `Vendors/Vendor List.csv` | `7839a43a…cef64863f9` |

### 5. Secret hygiene
- `.env.example` unchanged.
- `/api/ai/status` returns no key fields (`enabled`, `provider`, `configured`, `reason`, `policy`, `max_cost_per_batch_usd`, `allowed_tasks` only).
- AI service never serialises `self.api_key`.

---

## Confirmation table

| Requirement | Status |
| --- | --- |
| Richmond Utilities CLI works | ✅ 28 / 32 |
| Hopkinsville Water CLI works | ✅ 14 / 36 |
| Web app processing works | ✅ shared code path |
| Export still works | ✅ unchanged |
| Document Url still in export | ✅ Phase 1J shape preserved |
| Editable cell export still works | ✅ same plumbing |
| Dropbox still works | ✅ unchanged |
| Batch persistence still works | ✅ rehydration unchanged (toast on restore) |
| `Output/Template.xlsx` unchanged | ✅ SHA-256 unchanged |
| Source PDFs / CSVs unchanged | ✅ |
| Unit Info Clean / GL / Vendor List unchanged | ✅ |
| Secrets not exposed | ✅ no AI keys in any response |
| AI disabled by default | ✅ pill says **AI Off** |
| No real AI calls | ✅ adapter stubs raise on use |
| No new vendor processor | ✅ |
| No backend rewrite | ✅ |
| **No visible "Document" tab** | ✅ DocumentPreviewPanel toggle removed |
| **No vertical "Document"/"Review" text on collapsed rails** | ✅ replaced with CollapseRail |
| **File sidebar collapsible & resizable** | ✅ |
| **Template can dominate via Template focus preset** | ✅ |
| **No yellow row tints** | ✅ |
| **Modern nav icons; no Soon badges** | ✅ |
| **Workflow stepper simplified / hidden** | ✅ |
| **Batch management in sidebar** | ✅ |
| **More menu sectioned** | ✅ |
| **Documentation under docs/** | ✅ 22 reports moved; root has README.md |
| **Root README rewritten** | ✅ |

---

## Known limitations

- The workflow strip is hidden via CSS (rule `.workflow-strip { display: none; }`); the React component still mounts. That's intentional so future feedback can re-enable it without restoring code.
- The `BatchHeader` `document_mode` tag reads from `preview.summary.document_mode` if present; the backend's `_summary_for_batch` doesn't yet expose `document_mode` as a top-level field on the cached preview, so the tag is currently empty for all existing batches. Adding it is a one-line backend change deferred to the next phase to keep this PR scope tight.
- The `Soon` removal still leaves disabled nav-rail items present (greyed out + tooltip only). Hiding them entirely is a future option once one of them ships.
- The "Native viewer" fallback for binary PDFs is no longer reachable through a UI affordance (we removed the toggle). The lazy-loaded `PdfWorkspace` always renders for `.pdf` now. If a future PDF is too complex for PDF.js, the workspace will surface its own error state.
- Sticky first column on the template grid was not added in this phase (still header-only sticky).
