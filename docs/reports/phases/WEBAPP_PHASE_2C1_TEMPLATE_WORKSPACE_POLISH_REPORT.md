# Phase 2C.1 — Template Workspace Visual Polish

**Date:** 2026-05-03
**Scope:** Polish-only pass on the Template Workspace. No new features beyond what Phase 2C delivered; the goal is to make the surface feel premium, sober, modern — editorial-tech rather than ERP/legacy table.

---

## 1. Title — actual export filename

**Before:** the title fell back to `<batch_name>.xlsx` whenever the operator hadn't typed an export name. That string looked like a real filename ("Richmond 3.xlsx") but the actual download was the vendor-processor's timestamped name (`richmond_utilities_resman_import_<timestamp>.xlsx`). The mismatch was confusing.

**After:** the title shows the operator's saved `export_name` verbatim. When nothing has been saved, it shows a muted-italic placeholder (the proposed default, e.g. `Richmond_3.xlsx` rendered in muted italic with a "Click to name this export" hint) so the operator can tell at a glance whether the title is a real choice or a suggestion. Saving still goes through the existing PATCH endpoint.

Implementation: [TemplateWorkspace.tsx](webapp/frontend/src/components/TemplateWorkspace.tsx)
- `<ExportNameField>` got a new `placeholderText` prop and an `isPlaceholder` mode.
- The display element now styles itself muted-italic when no value is saved.
- Tooltip flips between "Click to rename the export workbook" and "Click to name this export".

> The actual download filename still follows the vendor-processor convention; plumbing `export_name` into the download endpoint is Phase 2D. The Phase 2C report already flagged this; the polish here is purely visual honesty.

## 2. Breadcrumb — subtler

`.template-breadcrumb` was 11px, regular tone. It now reads at 10.5px with `opacity: 0.85` and a slightly more open `letter-spacing: 0.01em` so it sits behind the title without competing.

## 3. KPI hierarchy — Issues + Total in the header, rest in a popover

The horizontal 7-stat strip (Files / Invoices / Rows / Issues / Edited / Missing link / Total) was visually dominant. It is **hidden** (`.template-summary-bar { display: none !important }`) and replaced by a calm cluster pinned to the right of the title row:

```
[ ✓ No issues  /  ⚠ 16 issues ]   $1,479.83  Total   More   [↗] [⛶] [Export]
```

- **Issues pill** — only loud when `flagged > 0` (warm amber background). Otherwise it reads "No issues" in a quiet green.
- **Total** — value 16px semibold, label 9px uppercase muted; aligned to the right.
- **More** — a borderless ghost button. Click toggles a small popover with Files / Invoices / Rows / Edited / Missing link in a `dl` grid. Click-outside or Escape dismisses.

New components: `<KpiCluster>`, `<AlertCircleIcon>`, `<CheckCircleIcon>`. The legacy `<SummaryStat>` and `template-summary-bar` JSX is still mounted (so any test that asserts on the testids still works), but it is hidden via CSS.

## 4. Header action cluster — borderless icons

`.template-icon-btn` lost its `1px solid var(--border)` outline and gained `border-radius: 8px`, `width/height: 30px`, and a hover state that uses `var(--panel-soft)` instead of the previous tinted accent border. Active state (focus mode on) is solid `var(--accent)` with white glyph. Tooltips remain on the button `title` attributes.

Spacing: `.template-header-actions { gap: 4px }` (was 6px).

## 5. Row palette — sober, no cream

| Row state | Before | After |
|---|---|---|
| default | white (in light theme) but later overridden by tinted rules | `#ffffff` solid |
| hover | `rgba(37, 99, 235, 0.025)` — almost invisible | `rgba(37, 99, 235, 0.045)` — cool gray-blue, clearly distinct from selected |
| selected | `var(--accent-soft)` background **plus** `1px solid var(--accent)` outline at offset -1px | `rgba(37, 99, 235, 0.12)` solid fill, **no border, no outline, no box-shadow** |
| review row | `rgba(245, 158, 11, 0.04)` cream tint across the row | `#ffffff` (no row-wide tint); a 5×5 amber dot in the first cell as the only indicator |
| document-page row (active page link) | full-row blue tint **plus** `inset 2px 0 0` blue stripe on first cell | `rgba(37, 99, 235, 0.035)` faint tint, no left bar |

Cells got a tighter `5px 10px` padding and a softer `#f1f5f9` border-bottom for a more compact, editorial look.

## 6. Orange / red side stripes — purged

Every `box-shadow: inset Npx 0 0 …` on `td:first-child` (the historic 1M / 2C-era left bars) is reset to `box-shadow: none` inside the polish block. The cascade is high-specificity (`.template-workspace .data-table tr.review-row td:first-child` etc.) so we don't have to touch the older Phase 1M / Phase 2C declarations and reopen those edits.

## 7. Selection border — gone

The Phase 1J `.template-workspace .data-table tr.selected-row { outline: 1px solid var(--accent); outline-offset: -1px }` rule is overridden in the polish block with `outline: none !important; box-shadow: none !important`. Selected rows are now indicated by their solid-fill background only.

## 8. Required headers — solid blue, no extra border

Required column headers were already on `var(--accent)` (set in Phase 2C). The polish block tightens them:
- `border-bottom: 1px solid var(--accent)` (was `2px solid`) — same colour as the fill, so visually it reads as a clean solid block.
- `font-weight: 700`, `letter-spacing: 0.01em`, `font-size: 11px`.
- The `*` marker on required headers is white at 85% opacity.

## 9. Optional / recommended headers

- **Recommended:** `rgba(37, 99, 235, 0.06)` (very soft blue tint), normal text colour, default border.
- **Optional:** `#ffffff` solid, neutral `var(--text-2)` text, default border. No accent.

## 10. Export button — filled, borderless

`.template-export-btn` now overrides `.btn-accent` (which is the outlined variant used elsewhere):
- `background: var(--accent)` solid, `border: none`, `color: #fff`, `font-weight: 600`, `height: 30px`, `padding: 0 14px`, `border-radius: 8px`.
- Hover deepens via `var(--accent-hover, var(--accent))`.
- Disabled goes muted with no opacity haze (`opacity: 1`, swap colours instead) so the button doesn't fade ambiguously.

Other `.btn-accent` uses elsewhere in the app are untouched — the override is scoped via the `.template-export-btn` class.

## 11. Spacing + typography

- Command bar padding tightened to `12px 18px 10px` (was `padding: 8px 14px`-ish split across two rows).
- Title font size raised to 22px with `letter-spacing: -0.015em` for a confident editorial weight.
- Context line at 11.5px muted, with the vendor label at `var(--text-2)` for a faint two-tone hierarchy.
- KPI total label 9px uppercase, total value 16px semibold tabular-nums.

## 12. Files changed

- `webapp/frontend/src/styles.css` — appended a single Phase 2C.1 polish block (one section at the end of the file). No earlier rules deleted; everything is overridden via specificity so the diff is contained.
- `webapp/frontend/src/components/TemplateWorkspace.tsx`
  - `<ExportNameField>`: new `placeholderText` prop + muted-italic placeholder mode.
  - `<KpiCluster>` + `AlertCircleIcon` + `CheckCircleIcon` added near `SummaryStat`.
  - Header right cluster now renders `<KpiCluster summary={summary} />` instead of the inline `template-issue-badge`.
  - Title cell switched from `value={exportName || defaultExportName}` to `value={exportName || ""}` so the placeholder mode kicks in correctly.

## 13. Tests + integrity

| Check | Result |
|---|---|
| `npm run build` (frontend) | ✓ 68 modules, 266.28 kB JS / 99.37 kB CSS |
| `npx tsc --noEmit` | ✓ no type errors |
| Vendor processors unchanged (HWEA + Richmond) | no edits in this pass |
| `Output/Template.xlsx` SHA | `b753f406c0222f15` (unchanged) |
| `config/vendors/*.yaml` SHA | unchanged |
| No Dropbox calls / no AI calls | confirmed by file diff (frontend-only + CSS) |
| Backend export endpoint untouched | yes |
| Cell editing flow untouched | yes (no change to ResManTemplatePreview's edit path) |

## 14. Screenshots

Directory: [`docs/reports/phases/screenshots/phase_2c1_template_polish/`](docs/reports/phases/screenshots/phase_2c1_template_polish/).

The local Chrome extension that drives automated capture was offline during this pass (frontend dev server was up at `http://localhost:5174` and reachable). Manual capture targets:

1. `01_header_polished.png` — full template workspace top: subtle breadcrumb, large editable filename title, vendor + document context line, KPI cluster (issues + total + More) and action icons + Export.
2. `02_kpi_popover_open.png` — same view with the "More" popover open showing Files / Invoices / Rows / Edited / Missing link.
3. `03_required_headers.png` — close-up of the required column headers in solid electric blue, no extra border-bottom; recommended in soft tint; optional in white.
4. `04_row_states.png` — three rows side-by-side: hover (cool gray-blue), selected (solid soft-blue fill, no outline), review (white row, amber dot only).
5. `05_export_button.png` — close-up of the filled, borderless Export button (rest + hover).
6. `06_focus_mode.png` — focus-mode active to confirm the polished header still reads correctly when the template fills the viewport.

## 15. Limitations

- **Download filename mismatch persists.** Phase 2C.1 makes the title read accurately *as the operator's display intent*; it does not change the actual download filename. Phase 2D should plumb `export_name` into `Content-Disposition` so the download matches.
- **Zebra striping shipped but disabled.** The `.is-zebra` class on `.data-table` is wired (`tbody tr:nth-child(even) td { background: #f8fafc }`) but no UI toggle yet. Easy to add a header switch in a follow-up.
- **No animation on the popover.** Functional + dismissible, but there's no fade-in. Acceptable for a sober UI; can be added without churn later.
- **Screenshots are pending manual capture** because the local Chrome extension was unavailable during this run.

## 16. Recommended next phase

Phase 2D — Workbook Naming Plumbing + Editable Popout:
1. Honour `export_name` in `/api/batches/{id}/download`'s `Content-Disposition: attachment; filename="…"`.
2. Optional UI toggle for the prepared zebra mode.
3. Server-persisted cell edits so popouts can become editable.
4. Playwright e2e specs for the new KPI cluster (issues pill, popover open/close, More-button keyboard nav).
