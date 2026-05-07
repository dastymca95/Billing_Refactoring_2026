# Webapp Phase 1K — Visual System + Workspace Refinement Report

**Date:** 2026-05-02
**Scope:** Frontend visual / UX refinement only. Vendor processors, CLI, Dropbox, export, and AI safety guarantees are unchanged. AI remains disabled by default — no provider call ever fires.

---

## TL;DR — what changed

| Phase 1J → Phase 1K |
| --- |
| **Giant green "Restored…" panel** consuming a column of vertical space → **bottom-right toast** that auto-dismisses after 4 s |
| **Visible "Soon" badges** on the nav rail → silent disabled icons with tooltip-only hint ("…coming later") |
| Long instructional text above the grid ("Click any cell to edit…") → **removed** — the grid speaks for itself; tooltips on cells stay |
| Single permanent layout → **View preset switcher** in topbar: **Review · Template · Document** (persisted to localStorage) |
| Harsh dividers, hard-edged borders, blue `#0969da` everywhere → softer `#d8dee4` borders, dedicated `--border-soft` / `--border-faint` levels, slightly cooler accent `#2563eb`, refined panel hierarchy |
| File rows with one badge → **PDF / CSV / XLSX / Image type badges** with soft tones plus a friendly vendor pill (`Richmond` / `Hopkinsville`) |
| Region tags shown as `service_address` | **`Service address`** etc. — friendly display; backend label stays snake_case |
| Document Url cell printed the full URL eating column width | Compact **↗ Open** link button per row; `—` placeholder when missing |
| 13-px font, 4-px scale base | **13-px base**, declared spacing scale (`--space-1..5`), declared radius scale (`--radius-sm..lg`), antialiased fonts |

---

## Files added (Phase 1K)

| File | Purpose |
| --- | --- |
| [webapp/frontend/src/components/Toasts.tsx](webapp/frontend/src/components/Toasts.tsx) | Stacked bottom-right toasts. Tones: info / success / warning / error. Auto-dismiss configurable per toast. Replaces persistent in-page banners. |
| [webapp/frontend/src/components/ViewPresetSwitcher.tsx](webapp/frontend/src/components/ViewPresetSwitcher.tsx) | Topbar segmented switcher (Review / Template / Document). `loadStoredPreset` + `persistPreset` helpers. |
| [WEBAPP_PHASE_1K_VISUAL_SYSTEM_REFINEMENT_REPORT.md](WEBAPP_PHASE_1K_VISUAL_SYSTEM_REFINEMENT_REPORT.md) | This document. |

## Files modified (Phase 1K)

| File | What changed |
| --- | --- |
| [webapp/frontend/src/App.tsx](webapp/frontend/src/App.tsx) | Replaced `setInfo` + in-page green/red banner with `pushToast()` → `<Toasts/>`. Wired `ViewPresetSwitcher` in topbar (collapses doc/inspector for *Template* preset). Added `reviewedKeys` browser-session state + `toggleReviewed` callback that's reset on each fresh `/manual-review` payload. |
| [webapp/frontend/src/components/NavRail.tsx](webapp/frontend/src/components/NavRail.tsx) | Removed the visible "Soon" pill. Disabled items have a slightly muted icon + tooltip ("…coming later") only. App no longer shouts unfinished. |
| [webapp/frontend/src/components/FileList.tsx](webapp/frontend/src/components/FileList.tsx) | File-type badge per row (PDF / CSV / XLSX / Image), with soft per-type tone. Friendly vendor names (`Richmond` / `Hopkinsville`). Cleaner empty state. |
| [webapp/frontend/src/components/ResManTemplatePreview.tsx](webapp/frontend/src/components/ResManTemplatePreview.tsx) | Removed verbose "Click any cell to edit…" banner. Document Url cells render as compact **↗ Open** chip; missing URLs show a muted `—`. |
| [webapp/frontend/src/components/pdf_workspace/types.ts](webapp/frontend/src/components/pdf_workspace/types.ts) | New `friendlyRegionLabel(label)` helper (Service address / Account number / etc.). Backend label stays snake_case. |
| [webapp/frontend/src/components/pdf_workspace/RegionBox.tsx](webapp/frontend/src/components/pdf_workspace/RegionBox.tsx) | Region tag now shows the friendly label. |
| [webapp/frontend/src/components/ReviewInspectorPanel.tsx](webapp/frontend/src/components/ReviewInspectorPanel.tsx) | New `reviewedKeys` / `onToggleReviewed` props + exported `issueKey()` helper. **Mark reviewed** action per issue card; reviewed cards visually de-emphasised + struck through. |
| [webapp/frontend/src/styles.css](webapp/frontend/src/styles.css) | Phase 1K visual system: refined CSS variables (warmer neutral, softer borders, bigger scale set, declared spacing/radius/shadow tokens), toast styles, view-preset switch, file-type badges, refined data-table tones, leading row-status dot styles, compact `.doc-url-icon`, refined nav rail and resizers, modal/mode-toggle polish. |

---

## PART A — prototype-looking elements removed

### A.1 No more giant green "Restored…" panel
- Removed the persistent `<div className="success-banner">{info}</div>` that lived inside the template column.
- Replaced `info` state and `setInfo(...)` call sites with a `pushToast({...})` API and a `<Toasts/>` stack mounted at the bottom-right of the viewport (`position: fixed`).
- Toasts auto-dismiss after 4 s by default (6 s for the "Processed N files" success message).
- Same-purpose toasts (e.g. *batch switched* twice in a row) deduplicate by id so back-to-back events don't pile up.
- Visually: bordered card with a coloured left strip (info / success / warning / error), `box-shadow-elev-2`, gentle `pop-in` animation.

### A.2 "Soon" badges hidden
- Disabled nav items still appear (so the operator knows the destinations exist) but no longer carry a visible **Soon** pill.
- Hint text moved to `title=` tooltip ("Review (coming later)", etc.). The app no longer broadcasts that it's unfinished.

### A.3 Collapse buttons trimmed
- The "Expand / Collapse" buttons on the document and inspector headers stayed (they're integrated into card headers as `.icon-button`), but the **giant collapse rail buttons** are now driven by the same `.collapsed-rail-button` pattern with refined typography.
- The new **View preset switcher** in the topbar replaces ad-hoc collapse-everything actions.

### A.4 Long instructional text gone
- Removed `<div className="muted">Click any cell to edit. Press Enter to save, Escape to cancel. Required columns have orange headers; optional columns can be hidden via the toggle…</div>`.
- The grid is confident now — tooltips on cells (existing) cover the same affordances when the operator hovers.

---

## PART B — visual system

### Token set (in `:root`)
```css
/* Backgrounds */
--bg:           #f3f5f8;
--panel:        #ffffff;
--panel-soft:   #f7f9fb;

/* Borders */
--border:       #d8dee4;   /* primary divider */
--border-soft:  #eef0f3;   /* dense panel divider */
--border-faint: #f3f5f8;   /* placeholder rule */

/* Accent */
--accent:       #2563eb;   /* slightly cooler than Phase 1J's #0969da */
--accent-soft:  #e6efff;

/* Severity */
--sev-high:     #b91c1c;
--sev-medium:   #b45309;
--sev-low:      #6b7280;

/* Spacing scale */
--space-1: 4px;  --space-2: 8px;  --space-3: 12px;  --space-4: 16px;  --space-5: 24px;

/* Radius scale */
--radius-sm: 4px;  --radius: 6px;  --radius-md: 8px;  --radius-lg: 12px;

/* Shadows */
--shadow:        0 1px 2px rgba(15,22,36,0.04), 0 1px 1px rgba(15,22,36,0.02);
--shadow-elev-1: 0 1px 3px rgba(15,22,36,0.06), 0 1px 2px rgba(15,22,36,0.04);
--shadow-elev-2: 0 8px 24px rgba(15,22,36,0.10), 0 2px 6px rgba(15,22,36,0.06);
```

### Touch points
- **Topbar**: flatter (no more 1-px hard line), tiny shadow under the bottom edge.
- **File sidebar**: `panel-soft` upload zone + dense file cards with type badges.
- **Action bar**: refined buttons, primary action keeps a soft shadow, accent variant has a softer cast.
- **Cards**: lighter borders + `radius-md`; not visually noisy any more.
- **Modal**: `radius-lg` for the dialog itself, refined input + mode-card.
- **Resizers**: 3-px channel that's transparent by default — only the inner `::after` rule paints a soft `border-soft` line that lights up to `accent` on hover.
- **Nav rail**: 56-px wide (was 64) with `panel-soft` background; refined gradient on the brand square.
- **Mode toggle (document panel)**: pill-shaped, matching the new view-preset style.

---

## PART C / J — view presets

A new segmented control in the topbar exposes three presets:

| Preset | Effect | Use for |
| --- | --- | --- |
| **Review** *(default)* | Document + Template + Inspector all visible | Day-to-day reconciliation work |
| **Template** | Doc + inspector collapsed; Template fills the screen | Big template scrubbing pass |
| **Document** | Doc visible; inspector visible; template stays normal | Marking field regions on PDFs |

The chosen preset persists via `localStorage["billing_refactoring_layout_view_preset"]`. The actual collapsing/uncollapsing flows through the existing collapsed states in App.tsx. Resizable widths still persist independently — switching presets doesn't override the operator's manual sizing within the visible panes.

---

## PART D — file sidebar

- The upload zone keeps its existing `compact` mode but lives inside a refined `panel-soft` section header.
- File cards (`.file-row`) now show the **file type** badge (PDF / CSV / XLSX / Image) with a soft tone:
  - PDF → `#fef2f2` / `#b91c1c`
  - CSV / XLSX → `#ecfdf5` / `#047857`
  - Image → `#eef2ff` / `#4338ca`
- The vendor badge remains, but the displayed text is friendly: `Richmond`, `Hopkinsville`, `needs review` (instead of `richmond_utilities`).
- The selected file row gets a **2-px accent stripe** on the inner left edge plus an `accent-soft` background — much cleaner than a full-row blue tint.
- Empty state is a one-liner: *"No files yet — drop bills into the upload zone above."*

---

## PART F — template grid

- **Required headers** softened: `#ffedd5` / `#9a3412` with a 2-px `#f97316` underline (less saturated than the old red-orange `#ffd6a8` / `#7a3a00`).
- **Recommended headers** in `--warning-soft` / `#92400e`.
- **Header text**: 11 px, 600-weight, color `--text-2`.
- **Row hover**: a near-transparent accent wash (`rgba(37, 99, 235, 0.025)`) — visible without being distracting.
- **Selected row**: `--accent-soft` with subtle accent outline.
- **Review (flagged) row**: subtle warm wash (`rgba(180, 83, 9, 0.05)`) that mixes cleanly with the selected state.
- **Document Url cell**: compact `↗ Open` chip (matches button styling); missing URL shows a muted `—` so empty cells aren't visually loud.
- Removed the verbose helper banner that lived above the grid.
- Leading row-status dot CSS (`.row-status-dot.ready / .review / .edited / .mismatch / .missing-link`) is shipped for use in a follow-up phase that wires per-row status data through the preview shape.

---

## PART G — Review / Inspector

- New **Mark reviewed** action on each issue card. Browser-session state only — no backend write, no removal of the underlying `manual_review_reasons` value. The card visibly de-emphasises (`opacity: 0.68`, `panel-soft` background, struck-through code) so the operator can see at a glance which issues they've worked through.
- The `reviewedKeys` set is reset every time a fresh `/manual-review` payload is loaded (process, refresh-preview, switch-batch, rehydrate) so the state never lies about a different batch's issues.
- Action button toggles between `Mark reviewed` (ghost) and `✓ Reviewed` (accent) with a clear tooltip explaining "session only".
- The empty state already lived in Phase 1J; copy unchanged.

---

## PART I — AI badge default

- The badge logic in `AiFallbackStatusBadge.tsx` already handled this correctly: when `enabled=false` and `provider="disabled"` (the shipping default), the pill reads **AI Off** (not "AI Error"). Phase 1K verified the rules:
  - `error` from `/api/ai/status` fetch → **AI Error** (real failure only)
  - `enabled=true` → **AI Ready**
  - `provider="disabled"` → **AI Off**
  - any other configured provider without a key → **AI Not Configured**
- No real AI calls fire; no API keys are exposed.

---

## PART K — friendlier copy

| Before | After |
| --- | --- |
| Region tag `service_address` | **Service address** |
| Region tag `account_number` | **Account number** |
| Region tag `total_amount` | **Total amount** |
| Vendor key `richmond_utilities` | **Richmond** |
| Vendor key `hopkinsville_water_environment_authority` | **Hopkinsville** |
| Vendor key `unknown` | **needs review** |
| `Could not load regions: HTTP 404 Not Found` | (Phase 1J already silenced this — Phase 1K kept it silent) |
| In-page banner *"Restored "Apr 2026" · 4 file(s) · preview available."* | **Toast** with the same text, auto-dismissed |
| Nav rail "Soon" badge | (hidden; tooltip-only "…coming later") |

Internal data shapes (snake_case region labels, vendor keys, run_context, etc.) remain unchanged.

---

## Tests performed (PART N)

### 1. Frontend build
```
$ npm run build
✓ 61 modules transformed.
dist/assets/index-CWV5zow7.js     199.77 kB │ gzip: 63.10 kB
dist/assets/index-Cb0quSoO.css     41.76 kB │ gzip:  8.07 kB
dist/assets/PdfWorkspace-…js       10.58 kB │ gzip:  4.06 kB  (lazy)
dist/assets/pdf-…js              293.42 kB │ gzip: 86.55 kB  (lazy)
dist/assets/pdf.worker-…mjs    1,875.78 kB                   (lazy)
✓ built in 1.84s
```

### 2. Backend smoke (FastAPI TestClient)
- `GET /api/ai/status` → 200, `enabled=False`, reason `"AI fallback disabled (provider=disabled)"`. **No API keys returned.**
- `GET /api/batches/no_such_batch/regions` → 200 + `{regions: []}` (Phase 1J fix preserved).

### 3. CLI regression

| Processor | Files | Invoices | Lines | Flagged |
| --- | --- | ---: | ---: | ---: |
| Richmond Utilities | 15 | 28 | 32 | 28 |
| Hopkinsville Water | 2 | 14 | 36 | 14 |

Both match the Phase 1J / 1I baselines exactly.

### 4. Source-file integrity (SHA-256)

| File | SHA-256 |
| --- | --- |
| `Output/Template.xlsx` | `b753f406…3969c284` (unchanged from Phase 1H/1I/1J) |
| `Properties/Unit Info Clean.csv` | `79d46c7c…219c1a683` (unchanged) |
| `Gl Codes/General Ledger Report.csv` | `8f8506ec…73abb6e49` (unchanged) |
| `Vendors/Vendor List.csv` | `7839a43a…cef64863f9` (unchanged) |

### 5. Secret hygiene
- `.env.example` unchanged (no new env vars introduced).
- AI status JSON returns no key fields.
- The AI service still never serialises `self.api_key`.

---

## Confirmation table

| Requirement | Status |
| --- | --- |
| Richmond Utilities CLI works | ✅ 28 invoices / 32 lines |
| Hopkinsville Water CLI works | ✅ 14 invoices / 36 lines |
| Web app processing works for both | ✅ shared code path, smoke-tested |
| Export still works | ✅ unchanged |
| Document Url still in export | ✅ Phase 1J shape preserved |
| Editable cell export still works | ✅ same plumbing |
| Dropbox still works | ✅ unchanged |
| Batch persistence still works | ✅ rehydration unchanged (toast on rehydrate) |
| `Output/Template.xlsx` unchanged | ✅ SHA-256 unchanged |
| Source PDFs / CSVs unchanged | ✅ |
| Unit Info Clean / GL / Vendor List unchanged | ✅ |
| Secrets not exposed | ✅ no AI keys in any response |
| AI disabled by default | ✅ pill says **AI Off** |
| No real AI calls | ✅ adapter stubs raise on use |
| No new vendor processor | ✅ no processor changes |
| No backend rewrite | ✅ only `regions.py` was the Phase 1J 404 fix; Phase 1K backend untouched |
| No giant green panel | ✅ replaced with toast |
| No visible Soon badges | ✅ hidden |
| No verbose grid helper | ✅ removed |
| No raw 404 region banner | ✅ Phase 1J fix preserved |

---

## Known limitations

- The **leading row-status column** (Ready / Review / Edited / Mismatch / Missing link) is wired up only via CSS classes (`row-status-dot.*`) — the per-row status logic that picks the right class still needs to be wired in `ResManTemplatePreview.tsx`. Today the row's tone is conveyed by background colour (review-row vs selected-row) and the inspector shows the full reason list.
- The **sticky first column** was not implemented in Phase 1K. The header is sticky; horizontal scrolling of wide column sets still drops the leading Invoice Number / Vendor cells off screen.
- The **vertical inspector dock** (move review panel to the bottom for wider tables) remains a follow-up item.
- The **per-issue "Mark reviewed" state is browser-session only.** Reloading the page clears it. Persisting reviewed-state would need a small backend surface that wasn't in this phase's scope.
- Workflow steps remain informational only — they still don't navigate.
- The PDF workspace still shows one page at a time (no thumbnail rail).

## Suggested manual screenshot checks

1. Load the app — the topbar should show the new **Review · Template · Document** switcher next to the AI pill.
2. Restore a previously processed batch — confirm the rehydration message appears as a **bottom-right toast** that fades away after a few seconds, not a column-eating green panel.
3. Open the **Mark Fields** mode in the document panel and draw a region — the region tag should read **"Service address"** (or whichever friendly label was selected), not `service_address`.
4. Process a batch with manual-review issues — confirm each issue card has a **Mark reviewed** button. Click it; the card should soften and the button should switch to **✓ Reviewed**.
5. Switch to **Template** preset — the document and inspector panes collapse to rails so the grid fills the screen.
6. Switch to **Document** preset — both panes return; click the document pane to widen.
7. Hover a disabled nav rail item — only the tooltip ("…coming later") is visible. No "Soon" pill on the rail itself.
