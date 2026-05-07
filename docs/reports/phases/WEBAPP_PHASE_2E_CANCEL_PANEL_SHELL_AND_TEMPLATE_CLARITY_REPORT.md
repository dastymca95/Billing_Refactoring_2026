# Phase 2E — Cancellation Fix, Panel Shell Consistency & Template Clarity

**Date:** 2026-05-03
**Scope:** Eliminate the double-confirm Stop dialog and the broken cancellation behaviour. Unify panel chrome so Batches / Document / Template visually align. Make revision history obvious. Fix the wrong filename in the title. Remove the unexplained yellow dots. Polish table padding, selection, and the Issues pill flow.

---

## 1. Native confirm root cause + fix

**Root cause.** Two paths existed in the Stop click chain:

1. **App.tsx::handleCancel** (line 880) — opens the app-native `ConfirmDialog`. Correct.
2. **TemplateWorkspace.tsx** (line 1140, the `<ProcessingPanel>` Stop button) — called `window.confirm("Stop processing this batch?")` *before* invoking `onCancel`. That's the dialog the user saw.

When `onCancel` then propagated up to App.tsx, the second confirm fired. Hence "double prompt."

A second `window.confirm` lived in `VendorRulesStudio.tsx::restore` (line 225).

**Fix.** Removed both. The Template panel's Stop button now calls `onCancel` directly; the parent's app-native ConfirmDialog is the single source of truth. VendorRulesStudio's Restore button now calls App.tsx's `requestConfirm` via a new prop (with `confirmLabel: "Restore"`).

Spec-aligned wording for the Stop dialog:

```ts
{ title: "Stop processing?",
  message: "Processing will stop at the next safe checkpoint.",
  confirmLabel: "Stop processing",
  cancelLabel: "Continue",
  tone: "danger" }
```

A regression test in `python` walks every `.tsx` / `.ts` source file (with comment-stripping) and asserts zero `window.confirm(`, `window.alert(`, `window.prompt(` calls remain. ✓ verified clean.

## 2. Cancellation root cause + fix

**Root cause.** Three bugs, each independent:

1. **OCR per-page loop ignored cancellation.** `utils/pdf_text_extractor.py::_try_ocr` only fired `progress_callback`. There was no `should_cancel` parameter. Once Tesseract started a 14-page run, it processed all 14 pages regardless of any cancel flag set by the operator.
2. **No-op cancellation produced a revision.** `_run_batch_in_background` recorded a revision unconditionally on success — even if the processor returned partial data because it had been asked to stop.
3. **Workbook still written on cancel.** Both vendor processors honoured `dry_run` but not `cancelled`, so the ResMan import xlsx, manual-review xlsx, and debug CSV were written from partial data.

**Fix.**

- `utils/pdf_text_extractor.py::_try_ocr` now takes `should_cancel: Optional[Callable[[], bool]]`. Polled before *every* OCR page; on a True it appends the warning `"ocr_cancelled"`, returns whatever pages already completed, and breaks out of the loop.
- `extract_pdf_text` plumbs the same `should_cancel` argument into `_try_ocr`.
- Hopkinsville and Richmond processors now pass their own `_should_cancel` through to `extract_pdf_text` (and through `parse_richmond_pdf_bill` for Richmond's PDF path).
- Both processors check `_should_cancel()` before the workbook-write block. If true, they skip ResMan / manual-review / debug writes (same code path as `dry_run`, with `reason="cancelled"` in the log).
- `processing.py::_run_batch_in_background` introduces `_was_cancelled(batch_id)` (checks the cancel registry first, then progress.json status) and `_stamp_cancelled(batch_id)` (uses `ProgressTracker.cancelled()` which marks running stages as `skipped` and writes `status=cancelled` + `percent=100`). On cancel: skip the cache write *and* the revision recording, then stamp the progress file.

**Acceptance:**

| Scenario | Before | After |
|---|---|---|
| Stop while running | OCR finishes all pages; revision created | OCR stops at next page boundary; no revision |
| Stop while queued | (no path; queue endpoint hadn't existed) | Removed from FIFO; runner never sees it |
| Stop after completion | "no_active_run" + nothing changes | Same — idempotent |
| Stop early in run | Workbook still written | No workbook, no revision, status=cancelled |
| `_webapp_result.json` | overwritten with partial data | preserved (the prior good preview) |

## 3. Queue + cancel behaviour

`processing_queue.cancel(batch_id)` returns one of:

- `removed_from_queue` — was queued, never started; UI marks it cancelled.
- `cancel_requested` — was running; the cancel_registry sets the tracker flag; the worker stops at the next OCR-page or per-file checkpoint.
- `not_running_or_queued` — idempotent no-op.

`cancel_endpoint` returns the same three states (mapped to friendly status strings) so the frontend knows whether to show "Stopping…" briefly or to flip straight to idle.

`/api/processing/queue` snapshot is unchanged (`{running, queued}`). The frontend polls every 1.5 s; per-batch chips pick up `Running` and `Queued` from this, and `Done` / `Failed` / `Cancelled` from each `BatchListEntry.status`.

## 4. Panel shell alignment

Before, the Template panel had `box-shadow: var(--shadow-elev-1)` while the Batches and Document panels had `box-shadow: var(--shadow)`. The radius and background already matched, but the shadow + the surrounding `template-area` wrapper (which had its own padding) made the Template panel sit slightly different.

Fix: extended the shared panel-shell rule in `styles.css:369`:

```css
.file-sidebar > .file-sidebar-card,
.document-pane > .card,
.document-pane > .doc-preview-card,
.template-and-inspector > .template-area > .template-workspace {
  flex: 1 1 auto;
  min-height: 0;
  height: 100%;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow);
  background: var(--panel);
  overflow: hidden;
}
```

And neutralised `.template-area` so it's a transparent flex pass-through (no padding, no border, no shadow of its own). All three panels now share one inner shell with the same chrome.

## 5. Window restore UX

A new "Windows" popover lives in the topbar (component: [`WindowsMenu.tsx`](webapp/frontend/src/components/WindowsMenu.tsx)).

```
┌─ Windows ▾ ──────────────┐
│ ☑ Batches                │
│ ☑ Document Viewer        │
│ ☑ Template               │
│ ─────────────────────────│
│   Restore all            │
└──────────────────────────┘
```

- Each row is a `role="menuitemcheckbox"` button.
- Checking/unchecking calls `App.tsx::setClosedPanels` to toggle visibility.
- "Restore all" clears the closed set in one go.
- Click-outside or Escape closes the popover.

The Phase 2D dock chip strip (`Restore: [Batches] [Document] [Template]`) stays as a fast-path shortcut when something is already closed. The WindowsMenu is now the canonical control because it works regardless of state.

## 6. Revision history visibility

The Revisions dropdown previously rendered only when `revisions.length > 0`, so on a fresh batch the operator had no idea the feature existed.

Now it's always rendered (when not in a popout / read-only host). The button label switches to `"Revision · No runs"` when the manifest is empty, and the popover shows a friendly "No revisions yet. Run Process to create one." instead of nothing.

The dropdown still:
- shows newest first
- marks the active revision with a blue dot + "Current" badge
- shows timestamp + invoice/row counts per entry
- activates on click

## 7. Export filename identity

Root cause: the frontend default was `<batch_name>.xlsx`. The backend default (`webapp/backend/api/export.py::_slug_for_default`) is `<slugified_batch_name>_ResMan_Import.xlsx`. The two strings diverged so the title showed e.g. `Richmond 3.xlsx` while the actual download landed as `Richmond_3_ResMan_Import.xlsx`.

Fix: `App.tsx::defaultExportName` now mirrors the backend slug routine byte-for-byte:

```ts
const raw = (batchName || "").trim() || "ResMan_Import";
let slug = raw.replace(/\s+/g, "_");
slug = slug.replace(/[\\/:\*\?"<>\|]+/g, "_");
slug = slug.replace(/^[.\s]+|[.\s]+$/g, "");
if (!slug) slug = "ResMan_Import";
return `${slug}_ResMan_Import.xlsx`;
```

That matches the backend's `_slug_for_default` → `_sanitize` chain output exactly. The placeholder shown in the title is now the same string the workbook will download as via `Content-Disposition`. The italic-muted treatment was also retired in Phase 2C.1; the title is now strong semibold throughout.

## 8. Issue dot explanation / removal

The yellow 5×5 dot (`background: rgba(245, 158, 11, 0.85)` on `tr.review-row td:first-child::after`) was added in Phase 2C.1. The user found it unexplained. It's gone in Phase 2E. The `.review-row` class is preserved in markup so the issues drawer / column-preset filtering still find flagged rows. The Issues pill in the topbar is now the single visible signal that some rows need attention.

## 9. Table polish

- First and last cell get `padding-left: 16px` / `padding-right: 16px` so invoice numbers no longer touch the edge.
- Selected row: solid soft-blue fill (`rgba(37, 99, 235, 0.10)`), no border, no outline, no stripe. Hover-on-selected deepens slightly to `0.14`.
- Hover-on-non-selected: `rgba(37, 99, 235, 0.045)` — subtle cool gray-blue, distinct from selection.
- Per-cell issue tint: `td.cell-issue` gets `rgba(245, 158, 11, 0.08)` (kept for missing required values), but the row stripe is gone.

## 10. Issues pill — already actionable

The audit confirmed the topbar IssuesPill already wires `onClick={() => setIssuesOpen(v => !v)}`. The new tooltip wording makes the action more discoverable — "Open issues panel" when there are issues, "No issues" when zero. No code change needed here beyond the cleanup the audit revealed.

## 11. Files touched

**Backend:**
- `utils/pdf_text_extractor.py` — `_try_ocr` and `extract_pdf_text` now accept `should_cancel`.
- `Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/process_hopkinsville_water_environment_authority.py` — passes `should_cancel` to `extract_pdf_text`; skips ResMan / manual-review / debug writes when cancelled.
- `Training Bills_Invoices/Water - Sewer/Richmond Utilities/process_richmond_utilities.py` — same: `should_cancel` plumbed through `parse_richmond_pdf_bill` to `extract_pdf_text`; cancel-aware workbook gate.
- `webapp/backend/api/processing.py` — `_was_cancelled`, `_stamp_cancelled`, `_run_batch_in_background` skips cache + revision on cancel.

**Frontend:**
- `src/components/TemplateWorkspace.tsx` — removed `window.confirm`; revisions dropdown always renders when not read-only; "No runs" label when zero revisions.
- `src/components/VendorRulesStudio.tsx` — removed `window.confirm`; restore now uses host's `requestConfirm`.
- `src/components/WindowsMenu.tsx` — new popover for panel toggles.
- `src/App.tsx` — Stop confirm dialog re-worded per spec; default export filename slugs match backend; `<WindowsMenu>` rendered in the topbar; `requestConfirm` plumbed to VendorRulesStudio.
- `src/styles.css` — yellow dot rule removed; shared shell rule extended to template-workspace; first/last cell padding bumped; selected-row outline removed; Windows menu styles; revisions-empty padding.

## 12. Tests performed

| Check | Result |
|---|---|
| `npm run build` | ✓ 68 modules, 276.52 kB JS / 108.32 kB CSS |
| `npx tsc --noEmit` (incremental) | ✓ no errors |
| `python -m compileall webapp/backend` | ✓ no errors |
| Native-dialog audit (live code, comment-stripped) | ✓ 0 hits |
| Queue smoke — submit A/B/C, cancel C | A and B ran, C never entered the runner |
| Cancel running smoke (HWEA 14-page batch) | `final progress status: cancelled`, **0 new revisions** |
| Revision count delta on cancel | `revs_before=2, revs_after=2` ✓ |
| Cancel idempotent on idle batch | `no_active_run` 200 |

**Integrity invariants:**

| File | SHA-256 (16) | Status |
|---|---|---|
| `Output/Template.xlsx` | `b753f406c0222f15` | unchanged |
| `Vendors/Vendor List.csv` | `7839a43a493a7c0c` | unchanged |
| `config/vendors/hopkinsville_water_environment_authority.yaml` | `e83c554709edd0bf` | unchanged |
| `config/vendors/richmond_utilities.yaml` | `6111d042658818d4` | unchanged |
| `config/vendors/backups/` | empty | unchanged |

No vendor extraction logic changed (only added cancellation checkpoints + cancel-aware write gates). No Dropbox calls. No AI calls. CLI behaviour unchanged: both processors still default `should_cancel_callback=None`, the OCR check is a no-op when not wired.

`npm run test:e2e` was not executed in this phase — Playwright specs for the new Stop confirm, Windows menu, revisions dropdown, and panel shell still need to be authored. Recommended for Phase 2F.

## 13. Screenshots

Directory: [`docs/reports/phases/screenshots/phase_2e_cancel_panel_shell_template_clarity/`](docs/reports/phases/screenshots/phase_2e_cancel_panel_shell_template_clarity/)

Pending manual capture (Chrome extension was offline during this run; dev stack confirmed up at 5174/8001):
1. `01_stop_confirm_app_native.png` — the Stop click opens the app-native dialog only.
2. `02_cancelled_state.png` — batch row shows "Cancelled" chip; status persists; no revision created.
3. `03_panel_alignment.png` — Batches / Document / Template panels lined up at top + bottom.
4. `04_windows_menu_open.png` — popover with checkmarks for the three modules + "Restore all".
5. `05_revisions_dropdown_open.png` — list of v1..vN with the active one marked Current.
6. `06_table_polished.png` — invoice numbers no longer touch the edge; selected row solid-fill blue; no stripe; no yellow dot.

## 14. Limitations

- **`should_cancel` polling granularity is per-OCR-page.** A page in the middle of Tesseract still finishes (Tesseract isn't interruptible from outside a thread). Worst-case latency is ~10–60 s on heavy scans. Acceptable; fully interruptible OCR would require subprocess-level kill, which risks corrupted state.
- **The frontend "Stopping…" button label** stays for the duration of the cancel-to-checkpoint window. We don't have a separate "stopping vs cancelled" UI distinction beyond the toast + the chip flipping to "Cancelled" on the next poll.
- **Per-cell `cell-issue` tint** is plumbed in CSS but no producer marks individual cells today — the issues drawer still works at the row level. Phase 2F+ can add per-cell metadata if needed.
- **Screenshots remain pending** because the Chrome extension was offline during the run.
- **CLI default behaviour is unchanged** — `should_cancel_callback` defaults to `None`. CLI runs continue to ignore cancellation entirely; webapp runs honour it.

## 15. Recommended next phase

Phase 2F — UX polish + e2e + per-cell issue metadata:
1. Author Playwright e2e specs for: Stop confirm (app-native only), cancel during running OCR, Windows menu toggling each panel, revisions dropdown active+inactive states, panel shell alignment.
2. Wire the per-cell `cell-issue` class from the existing manual-review reasons (e.g. `missing_required_*`).
3. "Cancelling…" → "Cancelled" toast with retry suggestion when the operator wants to retry a stopped run.
4. Optional: surface revision history activation in the toast: "Switched to v2 of N — preview refreshed."
