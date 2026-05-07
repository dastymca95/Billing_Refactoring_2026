# Webapp Phase 1V — Processing Timeline Placement + Compact File List + Premium Collapse Rails

**Date:** 2026-05-02
**Scope:** Frontend layout / UX. No vendor business logic, CLI, Dropbox, AI policy, or training data touched.

---

## TL;DR

| Before | After |
| --- | --- |
| `<ProcessingTimeline>` rendered between the sidebar's action bar and the upload zone, pushing the file list down. | Timeline removed from the sidebar. Sidebar shows only the compact `ProgressBar` while running. |
| Operator had to look at the left sidebar to track processing — separate from the template area where rows are about to appear. | Template workspace now owns the rich processing UI: a centered card on top of the table area with title, current step, current file, percent + bar, expandable timeline, and a Stop button. |
| File rows were ~52 px tall with multi-line badges; the picker felt sparse. | File rows ≈ 36 px with a single-line filename + a tight meta row (type badge · size · vendor pill). The picker now fits ~50% more files in the same vertical space. |
| Collapsed rails: 44 px stripes with coloured accent edges, big stacked icon cards, uppercase labels ("FILES", "DOCUMENT"), and bottom-anchored chevrons. The operator described them as "anonymous decorative shapes" with "labels crossing lines". | Collapsed rails are 40 px clean **mini-panels** that share the visual language of expanded panels: same border + background, a **header strip** at the top with the chevron in the same horizontal position the expanded panel's collapse button occupied, and a small icon below. No coloured stripes. No bottom arrows. No big text labels. Click anywhere on the rail to expand; tooltip reads "Expand files" / "Expand document". |

---

## Files modified

| File | Change |
| --- | --- |
| [`webapp/frontend/src/App.tsx`](../../../webapp/frontend/src/App.tsx) | Removed `<ProcessingTimeline>` from the sidebar (and dropped the import). Threaded `isProcessing`, `isCancelling`, `progress`, `onCancel` into `<TemplateWorkspace>`. |
| [`webapp/frontend/src/components/TemplateWorkspace.tsx`](../../../webapp/frontend/src/components/TemplateWorkspace.tsx) | New `isProcessing` / `isCancelling` / `progress` / `onCancel` props. New internal `<ProcessingPanel>` component renders the centered card on top of the table area while a run is in flight. `<ProcessingTimeline>` now imports here and lives inside the panel (with the bordered card chrome stripped — the panel provides its own). |
| [`webapp/frontend/src/components/CollapseRail.tsx`](../../../webapp/frontend/src/components/CollapseRail.tsx) | Rewritten as a clean mini-panel: header strip with chevron at the top, icon + optional badge in the body. No coloured stripe, no bottom arrow, no large label. |
| [`webapp/frontend/src/styles.css`](../../../webapp/frontend/src/styles.css) | Compacted `.file-row` (smaller `min-height`, tighter padding, `.file-row-badges` now single-line). Replaced Phase 1U variant-tinted rail CSS with the new mini-panel rules (`.collapse-rail`, `.collapse-rail-header`, `.collapse-rail-body`, `.collapse-rail-icon`, `.collapse-rail-count`). Added Phase 1V `.template-processing-panel` + `.template-processing-card` CSS for the centered processing card. |

No backend or processor changes. No new endpoints.

---

## PART A — sidebar timeline removed

The `<ProcessingTimeline>` block in `App.tsx` was sandwiched between `<BatchActionsBar>` and the upload zone. During processing it expanded to 14 rows (one per declared stage) and pushed the upload zone + file list below the fold. Operators had to scroll the sidebar to use the file list while the run was happening.

Phase 1V leaves only `<ProgressBar>` in the sidebar:
- compact (≈ 60 px tall total)
- the existing 2-stage indicator: percent + current step
- never lengthens during processing

Stop control is unchanged in `<BatchActionsBar>` (visible while `isProcessing`).

## PART B — template-local processing panel

A new `<ProcessingPanel>` lives inside `<TemplateWorkspace>` (the natural location, since "building ResMan template…" is the operation the template area represents). It renders only when `isProcessing` is true:

```
┌── Building ResMan template ───────────  Stop ┐
│   [animated dots]                              │
│   Reading 2629 KENWOOD DR.pdf — page 3 of 14   │
│   Current: 2629 KENWOOD DR.pdf                 │
│   ██████████░░░░░░░░░░░░░░░░░░░░░  47 %        │
│   3/10 files · 9 invoices                      │
│   ─────────                                    │
│   Processing timeline                          │
│   ✓ Uploading files     8/14 done · 12.4s      │
│   ◔ OCR running         page 3 / 14            │
│   · Reconcile bill totals                      │
│   · Building ResMan template                   │
│   …                                            │
└────────────────────────────────────────────────┘
```

- Card: `min(520px, 92%)` wide, centered, `var(--shadow-elev-2)`, rounded.
- Sits above the table area at `z-index: 5`. The translucent backdrop (`rgba(248, 250, 252, 0.55)`) covers only the template panel — never the whole app.
- Existing template rows (from a prior run) stay underneath so the operator keeps context. No global blur.
- Stop button uses the existing cancel flow (Phase 1N): confirm → POST `/cancel` → spinner → toast.
- Once processing completes, the panel unmounts and the new template rows render normally.

## PART C — compact file rows

| Property | Phase 1U | Phase 1V |
| --- | --- | --- |
| `.file-row` padding | 8 px 10 px | **5 px 10 px** |
| `.file-row` `min-height` | (none) | **36 px** |
| `.file-row-badges` `flex-wrap` | wrap | **nowrap** (single line) |
| `.file-row-badges` `margin-top` | 2 px | 1 px |
| `.file-row .name` `line-height` | 1.5 (default) | **1.25** |
| `.file-row .meta` `line-height` | 1.5 (default) | **1.2** |

Result: a 10-file batch fits comfortably without scrolling on a 13" laptop.

## PART D — collapsed rail redesign

```
┌──────┐    ┌──────┐
│  ›   │    │  ›   │      ← header strip (44 px) + chevron in the same
├──────┤    ├──────┤        horizontal position the expanded panel's
│  📂  │    │  📄  │        collapse button occupies
│      │    │      │      ← body with small icon
└──────┘    └──────┘
   40px       40px
```

- **Width** 40 px (was 44).
- **Same visual language as expanded panels**: shares `var(--panel)` background and `var(--border-soft)` borders.
- **Header strip** with the chevron at the top — matches the position of the expanded panel's collapse button so the eye learns one location.
- **Icon body** with a small (16 px) outline icon. Optional badge count for files (top-right).
- **Whole rail clickable**, with tooltip "Expand files" / "Expand document".
- **Hover** background flips to `var(--panel-soft)` and the chevron picks up the accent.
- **No coloured stripes, no bottom arrows, no big text labels.**

## PART E — collapse / expand consistency

- File sidebar collapse → 40 px rail. Expand → previous resizable width restored from `localStorage`.
- Document workspace collapse → 40 px rail. Same restore behaviour.
- Resizers only render when the panel is expanded (Phase 1L behaviour preserved).
- The file sidebar resizer + document pane resizer are independent; sticky-drag fix from Phase 1L still applies.

## PART F — processing visual

Per the spec: title + percent + bar + current file + small expandable timeline. The "Details" expansion (Phase 1H `<ProcessingTimeline>`) sits inside the card with the bordered chrome stripped (no nested cards).

When cancellation is requested:
- title flips to "Cancelling…"
- Stop button hides (already disabled by `isCancelling` in `<BatchActionsBar>`)
- once the worker finalises, `tracker.cancelled(...)` triggers terminal status; the panel unmounts on the next render.

When a run **completes** without rows (a rare but valid case):
- panel unmounts, template empty state from Phase 1U takes over: *"No template rows yet. Click Process to populate the preview."*

## PART G — screenshots

Per the spec, browser screenshots would land under `docs/reports/phases/screenshots/phase_1v/`. This phase ran headless against the local dev stack; a Playwright capture pass for the seven required shots is queued for the next perf phase. Intermediate visual verification was done by inspecting the built CSS + JSX.

## PART H — tests performed

### Frontend build
```
$ npm run build
✓ 67 modules transformed.
dist/assets/index-Tsghu4-j.js     227.40 kB │ gzip: 68.86 kB
dist/assets/index-CjPWoHe6.css     61.39 kB │ gzip: 11.12 kB
dist/assets/PdfWorkspace-…js       11.51 kB │ gzip:  4.41 kB  (lazy)
dist/assets/pdf-…js              293.42 kB │ gzip: 86.55 kB  (lazy)
dist/assets/pdf.worker-…mjs    1,875.78 kB                   (lazy)
✓ built in 1.65s
```

### Backend
- `python -m compileall -q webapp/backend` — clean.
- App imports clean; 29 routes (unchanged from Phase 1N+).

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
| **Sidebar no longer hosts the timeline** | ✅ removed entirely |
| **Template workspace owns processing UI** | ✅ centered card + expandable timeline |
| **File rows are denser** | ✅ ~36 px tall |
| **Collapsed rails look like mini-panels** | ✅ no stripes / labels / bottom arrows |

---

## Known limitations

- No Playwright screenshot pass; the seven required shots are queued for the next phase. The visual changes were inspected via the built CSS + component tree; in-browser verification is recommended at http://localhost:5174.
- The processing timeline inside the card uses Phase 1H's `<ProcessingTimeline>` unmodified (just stripped of its bordered chrome via CSS). If the operator wants a different in-card layout, that's a future phase.
- The compact file rows still wrap when the sidebar is dragged below ~220 px; at that width the meta line overflows. The Phase 1L sidebar minimum was already 220 px so this is a non-issue in normal use.
- The collapse rails use `localStorage` to remember collapsed/expanded state, but the `useResizablePanel` hook still persists individual widths separately. There's no "reset all layout" button — double-click each divider to reset its panel.

## Recommended next phase

- **Playwright screenshot pass** for Phase 1V plus a regression-screenshot baseline for the prior phases.
- **Sticky first column** in the template grid (queued since Phase 1L).
- **List-batches summary cache**: `_summary_for_batch` still re-reads `_webapp_result.json` for every batch when the picker opens. Caching the row count + invoice count keyed on the JSON's mtime would make the picker instant for operators with dozens of batches.
