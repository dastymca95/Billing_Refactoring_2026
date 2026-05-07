# Webapp Phase 1L — Product UI Simplification & Premium Finish Report

**Date:** 2026-05-02
**Scope:** Frontend simplification only. Vendor processors, CLI, Dropbox, export, batch persistence, and AI safety guarantees are unchanged. AI remains disabled by default — no provider call ever fires.

---

## TL;DR — what changed

| Phase 1K → Phase 1L |
| --- |
| **Always-visible right Issues panel** stealing template width | **Issues drawer overlay** triggered by an "Issues N" pill in the topbar |
| Vertical *"▶ Review"* rail label when collapsed | Removed entirely — drawer slides in/out, no rail |
| Visible "Collapse" / "Expand" text on every panel header | **Icon-only** chevron buttons with `title` tooltips |
| Toy 1-2-3-4 numbered circles for the workflow stepper | **Compact connected status pills** in a single rounded strip |
| Topbar "VIEW Review Template Document" segmented control | Cleaner switcher: **Review · Template focus · Document focus** (no "VIEW" prefix) |
| `AI Error` shown when `/api/ai/status` 404'd | **AI Off** + friendly hint copy. Errors only surface for actual provider failures (which can't happen yet — no real calls fire) |
| AI popover technical `.env` instructions front and center | Friendly first sentence; `.env` details tucked under a `<details>` collapsed section |
| Resize divider sticky-drag bug | Fixed (see [PHASE_1L_RESIZER_BUGFIX_REPORT.md](WEBAPP_PHASE_1L_RESIZER_BUGFIX_REPORT.md)) |

---

## Files added

| File | Purpose |
| --- | --- |
| [webapp/frontend/src/components/IssuesDrawer.tsx](webapp/frontend/src/components/IssuesDrawer.tsx) | Right-side overlay drawer. Wraps the existing `ReviewInspectorPanel` body so the issue cards / "Mark reviewed" behaviour from Phase 1K is preserved. Closes on X icon, Esc key, or backdrop click. |
| [webapp/frontend/src/components/IssuesPill.tsx](webapp/frontend/src/components/IssuesPill.tsx) | Topbar trigger — shows "**N** issue(s)" with severity tone (warn / error) or "**No issues**" (success) when the batch is clean. SVG icons for alert / check. |
| [WEBAPP_PHASE_1L_PRODUCT_UI_SIMPLIFICATION_REPORT.md](WEBAPP_PHASE_1L_PRODUCT_UI_SIMPLIFICATION_REPORT.md) | This document. |
| [WEBAPP_PHASE_1L_RESIZER_BUGFIX_REPORT.md](WEBAPP_PHASE_1L_RESIZER_BUGFIX_REPORT.md) | Standalone resizer bug-fix report (root cause / fix / tests). |

## Files modified

| File | Change |
| --- | --- |
| [webapp/frontend/src/App.tsx](webapp/frontend/src/App.tsx) | Removed the always-visible inspector pane + its resizer. Added `issuesOpen` state, `IssuesPill` in the topbar, `IssuesDrawer` mounted at the shell level. Template area now consumes the freed width. View preset side-effects simplified — only the Document pane is collapsed in *Template focus*; the drawer is opened explicitly by the operator. |
| [webapp/frontend/src/components/WorkflowSteps.tsx](webapp/frontend/src/components/WorkflowSteps.tsx) | Rewritten as a compact `<ol class="workflow-strip">` with status dots + label + small detail; chevron `›` separators; no large numbered circles. |
| [webapp/frontend/src/components/ViewPresetSwitcher.tsx](webapp/frontend/src/components/ViewPresetSwitcher.tsx) | Dropped the "View" label prefix. Renamed presets to **Review** / **Template focus** / **Document focus**. |
| [webapp/frontend/src/components/AiFallbackStatusBadge.tsx](webapp/frontend/src/components/AiFallbackStatusBadge.tsx) | A failed `/api/ai/status` fetch now shows **AI Off** (not "AI Error"). Friendlier popover copy ("AI assist is currently off…"); the `.env` hint moved into a collapsed `<details>` block. |
| [webapp/frontend/src/components/DocumentPreviewPanel.tsx](webapp/frontend/src/components/DocumentPreviewPanel.tsx) | Collapse button: text "Expand"/"Collapse" → SVG chevron + tooltip. |
| [webapp/frontend/src/components/ResManTemplatePreview.tsx](webapp/frontend/src/components/ResManTemplatePreview.tsx) | Same: chevron icon button with rotate-on-state. |
| [webapp/frontend/src/components/ReviewInspectorPanel.tsx](webapp/frontend/src/components/ReviewInspectorPanel.tsx) | Same: chevron icon button (still useful when the inspector is reused outside the drawer). |
| [webapp/frontend/src/hooks/useResizablePanel.ts](webapp/frontend/src/hooks/useResizablePanel.ts) | Sticky-drag bug fix — see the dedicated report. |
| [webapp/frontend/src/styles.css](webapp/frontend/src/styles.css) | Phase 1L additions: `.issues-pill`, `.drawer-backdrop` + `.drawer-right` slide-in animation, `.icon-btn` (single style for all icon-only buttons), `.workflow-strip` (replaces `.workflow-steps`), `.ai-pop-details` collapsed section. Old `.workflow-steps` rule kept but display:none. |

---

## PART A — fixed Issues panel removed

The right-side `aside.inspector-pane` is gone. In its place:

1. **Topbar pill**: `[⚠ N issues]` (warn) / `[● N issues]` (error tone if any reason matches `fail|error|missing.*total|total_mismatch|not_found|invalid`) / `[✓ No issues]` (clean). Click to open.
2. **Right-side drawer** with the same `ReviewInspectorPanel` body the Phase 1K panel rendered. Closes via:
   - The **X** icon button in the drawer header.
   - The **Escape** key.
   - Clicking the dimmed backdrop outside the drawer.
3. The drawer **does not steal width**. It overlays the workspace; the template grid keeps the full width whether the drawer is open or closed.
4. The vertical `▶ Review` rail label is gone entirely.

When an issue card's **Show row** action is clicked, the drawer stays open and the template row is selected/highlighted in the background grid.

## PART B — "Collapse" text gone

Every visible "Collapse" / "Expand" string was replaced with a chevron icon button (`<svg>` + `title=` tooltip + `aria-label`):

- Document panel header
- Template grid header
- Inspector panel header (used inside the drawer; still useful when reused)

A new uniform `.icon-btn` class provides consistent size (24×24), neutral-on-hover background, focus ring on keyboard focus.

## PART C — Document Workspace controls

The mode toggle (Document / Mark Fields) is unchanged in HTML — the CSS now renders it as a softer pill segmented control matching the new view preset switch. Region label dropdown only appears in Mark mode (already the case in `ViewerToolbar`).

## PART D — VIEW control simplified

The topbar segmented switcher used to read `VIEW Review Template Document`. In Phase 1L:

- Removed the `View` label prefix entirely.
- Renamed segments to **Review · Template focus · Document focus** (clearer about what they do).
- Side-effects on presets simplified: *Template focus* collapses the document pane; *Document focus* and *Review* leave it open. The drawer is no longer opened/closed by the preset switch — it's an explicit action.

## PART E — AI status

`AiFallbackStatusBadge` now distinguishes:

| Condition | Pill | Tone | Popover message |
| --- | --- | --- | --- |
| `enabled=true` | **AI Ready** | green | "AI assist is ready. It will suggest values only when rules and OCR confidence is low." |
| `provider="disabled"` | **AI Off** | grey | "AI assist is currently off. The app is using rules, OCR, YAML, and validation only." |
| Provider chosen but no key | **AI Not Configured** | amber | "A provider is selected but no API key is set yet." |
| `/api/ai/status` request **fails** | **AI Off** | grey | Same friendly off message. *(The previous version showed "AI Error" — that was misleading because a failed status fetch is a deployment/config issue, not a runtime AI failure.)* |
| Loading | **AI…** | grey | "Checking AI configuration…" |

Provider configuration details (`AI_FALLBACK_ENABLED=true`, `AI_PROVIDER=…`, the matching API key) are now tucked behind a `<details>` collapsed section inside the popover labelled **Configuration**, so the primary message stays product-friendly.

## PART F / G — workflow stepper redesign

```
┌─────────────────────────────────────────────────────────┐
│  ●  Upload · 4 files  ›  ●  Process · 12 invoices  ›    │
│  ▲  Review · 3 issues  ›  ●  Export · Ready             │
└─────────────────────────────────────────────────────────┘
```

- One single rounded `.workflow-strip` (a `<ol>`) with horizontal items.
- Each item has a status dot (gray / blue / green / amber) + bold label + grey detail.
- Active step gets a soft halo around its dot.
- Items are joined by a thin `›` chevron separator.
- Compact (~26 px tall), fits the topbar without dominating it.

## PART H — template dominance

By default the template area now occupies the full remaining width (after sidebar + document pane). The right-side inspector column that used to claim 360 px is gone; the only thing that overlays the template is the drawer, and only when the operator opens it.

In Template focus mode the document pane also collapses, giving the grid the entire post-sidebar width.

## PART I — drawer design polish

The drawer body reuses `ReviewInspectorPanel`, so the issue card layout, severity dots, **Mark reviewed** action, and Selected-row inspector are all preserved from Phase 1K. The drawer adds:

- Sticky header with the "Issues N" title and an icon-only close button.
- Soft slide-in animation (`drawer-slide` cubic-bezier 0.16,1,0.3,1, 220 ms).
- Backdrop fade (16 ms) — `prefers-reduced-motion` skips both animations.

## PART J — file sidebar

Phase 1K's file cards (file-type badge + friendly vendor name + clean truncation) are preserved; no further changes needed in this phase. The drop zone keeps its `compact` mode and the new soft `panel-soft` background introduced in Phase 1K.

## PART K — copy refresh

| Before | After |
| --- | --- |
| `AI Error` | **AI Off** (and "AI Error" reserved for true runtime failure only — currently unreachable) |
| Popover: *"AI fallback disabled or not configured"* | *"AI assist is currently off. The app is using rules, OCR, YAML, and validation only."* |
| `What AI would help with` | `What AI assist can help with` |
| Drawer header `Manual review` (Phase 1J) | **Issues** |
| Topbar pill *(none)* | **N issues** / **No issues** |
| Vertical rail label `▶ Review` (Phase 1J) | *(removed)* |
| `View` label in segmented switcher | *(removed)* |
| `Field regions` (mode toggle title) | already **Mark Fields** in Phase 1J/1K |

Internal data shapes (snake_case region labels, vendor keys, run_context, etc.) remain unchanged.

---

## Tests performed (PART N)

### 1. Frontend build
```
$ npm run build
✓ 63 modules transformed.
dist/assets/index-Du8-wK-W.js     204.34 kB │ gzip: 64.03 kB
dist/assets/index-74JoFZ0i.css     45.80 kB │ gzip:  8.69 kB
dist/assets/PdfWorkspace-…js       10.58 kB │ gzip:  4.07 kB  (lazy)
dist/assets/pdf-…js              293.42 kB │ gzip: 86.55 kB  (lazy)
dist/assets/pdf.worker-…mjs    1,875.78 kB                   (lazy)
✓ built in 1.67s
```

### 2. Backend smoke (FastAPI TestClient)
- `GET /api/ai/status` → 200, `enabled=False`, `provider="disabled"`. **No API keys in response.**
- `GET /api/batches/no_such/regions` → 200 + `{regions: []}` (Phase 1J fix preserved).

### 3. CLI regression

| Processor | Files | Invoices | Lines | Flagged |
| --- | --- | ---: | ---: | ---: |
| Richmond Utilities | 15 | **28** | **32** | 28 |
| Hopkinsville Water | 2 | **14** | **36** | 14 |

Both match the Phase 1H/1I/1J/1K baselines exactly.

### 4. Source-file integrity (SHA-256)

| File | SHA-256 |
| --- | --- |
| `Output/Template.xlsx` | `b753f406…3969c284` (unchanged across all phases) |
| `Properties/Unit Info Clean.csv` | `79d46c7c…219c1a683` (unchanged) |
| `Gl Codes/General Ledger Report.csv` | `8f8506ec…73abb6e49` (unchanged) |
| `Vendors/Vendor List.csv` | `7839a43a…cef64863f9` (unchanged) |

### 5. Secret hygiene
- `.env.example` unchanged (no new env vars in this phase).
- AI status JSON returns no key fields.
- `/api/ai/status` reachable via TestClient: `enabled=False`, `provider="disabled"`. Confirmed no keys leak.

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
| No backend rewrite | ✅ no backend changes in 1L (`regions.py` 1J fix preserved) |
| No fixed inspector pane | ✅ replaced with drawer |
| No vertical "Review" rail | ✅ removed |
| No visible "Collapse" text | ✅ icon-only buttons |
| No "AI Error" by default | ✅ shows "AI Off" on fetch failure |
| Resize sticky-drag bug | ✅ fixed (see `WEBAPP_PHASE_1L_RESIZER_BUGFIX_REPORT.md`) |

---

## Known limitations

- The drawer is right-edge only; a docking system that lets the operator move the drawer to the bottom is not part of this phase.
- The drawer cannot be torn off into a floating window.
- The topbar density on very narrow viewports (<1080 px) can still wrap the workflow strip below the brand. This is acceptable for the supported desktop workflow.
- Sticky first column on the template grid was not added in this phase (still header-only sticky).
- Real AI provider HTTP calls are still not wired — Phase 1L is UX simplification only.
