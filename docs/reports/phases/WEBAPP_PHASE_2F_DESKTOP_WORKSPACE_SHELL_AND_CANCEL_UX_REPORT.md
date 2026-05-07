# Phase 2F - Desktop Workspace Shell Cleanup + Docked Minimize System

**Date:** 2026-05-03
**Project:** Billing Refactoring 2026 Web Console
**Stack verified:** Backend `http://localhost:8001`, frontend `http://localhost:5174`

## 1. Operational QA findings

The app was opened in the browser and exercised as an operator across the Batches, Document Viewer, Template, processing controls, cancellation flow, Windows menu, minimize/restore/close actions, maximize/restore, batch switching, revisions, export controls, and issues UI.

Confirmed pre-fix problems:

| Area | Finding | Impact |
|---|---|---|
| Cancelled processing | A cancelled state could remain visually pinned in the Batches panel as a large progress/status card. | The panel looked stuck and consumed space after the operation was already done. |
| Minimize behavior | Minimized modules became vertical rails/columns inside the main workspace. | The shell looked unfinished and squeezed the remaining panels. |
| Window recovery | Closed/minimized panels were recoverable through disconnected topbar chips and older menu behavior. | Operators had no clear desktop-style model for visible, docked, and closed panels. |
| Template chrome | Template window controls were not in the same structural place as Batches and Document Viewer. | The Template module looked detached from the rest of the workspace. |
| Template header | Export filename, revision, stats, issues, export, and window controls competed in one crowded area. | At narrower widths the header became visually compressed. |
| Top app shell | The old `Windows` button and global issue/AI indicators felt disconnected from the app frame. | The shell did not read as a coherent premium desktop workspace. |
| Revision dropdown | Empty revision state used confusing copy such as `No runs`. | Operators could think preview data was missing even when the current preview was present. |

Processing screenshots and the E2E cancellation capture used safe Playwright route stubs. Backend queue/cancel/revision behavior was separately smoke-tested with `TestClient` and monkeypatched processing so vendor processors, Dropbox, source bills, and AI providers were not invoked.

## 2. Cancelled-state fix

Changed the frontend cancellation presentation so cancellation is transient and operationally clear:

- `ProgressBar` now treats `cancelled` as a final state and does not pin a large completed/cancelled card in the Batches panel.
- Cancellation now produces a toast: `Processing cancelled.`
- After a short delay, the progress state clears if it still belongs to the cancelled batch.
- Batch rows no longer preserve a prominent cancelled chip as the primary state; cancelled batches settle back to ready/idle presentation so the Process action is available again.
- The backend progress history can still preserve cancelled state; the UI simply no longer treats it as active panel content forever.

Files touched:

- `webapp/frontend/src/components/ProgressBar.tsx`
- `webapp/frontend/src/App.tsx`
- `webapp/frontend/src/components/BatchExplorer.tsx`

## 3. Dock/taskbar minimize model

Removed the vertical minimized rail behavior from the active App shell.

New behavior:

| Action | Result |
|---|---|
| Minimize | Panel disappears from the workspace and appears in a bottom dock/taskbar. |
| Restore from dock | Panel returns to the normal workspace. |
| Close | Panel is hidden completely and is not shown in the dock. |
| Restore from Windows menu | Closed or docked panel is made visible again. |
| Maximize | Panel enters focus mode. |
| Restore | Focus mode exits and normal workspace geometry returns. |

Dock details:

- Fixed bottom dock appears only when at least one panel is minimized.
- Dock items show icon plus label for `Batches`, `Document Viewer`, and `Template`.
- Dock items restore the corresponding panel.
- No vertical minimized columns or empty rails remain in the active App shell.

Primary files:

- `webapp/frontend/src/App.tsx`
- `webapp/frontend/src/styles.css`

## 4. Top app shell and Windows menu redesign

The topbar now reads more like a desktop application shell:

- Left brand remains `Billing Refactoring` / `WEB CONSOLE`.
- Command area contains `Workspace | Windows | View`.
- `Windows` opens the panel-management menu.
- `View` restores all workspace panels.
- The global issues pill is only shown when there are actual issues and remains actionable by opening the Issues drawer.
- `AI Off` is quieter and no longer visually dominates the right side.

The Windows menu now reports each panel state:

- `Visible`
- `Docked`
- `Closed`

Menu actions:

- Click a visible item to close it.
- Click a docked or closed item to restore it.
- `Minimize all`
- `Restore all`

Primary files:

- `webapp/frontend/src/components/WindowsMenu.tsx`
- `webapp/frontend/src/App.tsx`
- `webapp/frontend/src/styles.css`

## 5. Standardized panel shell

The Batches, Document Viewer, and Template panels now share a consistent window model:

- Window controls are right-aligned in the panel chrome.
- Control order is minimize, maximize/restore, close.
- Controls are icon-only and use consistent sizing.
- Close hover is subtle: gray hover background with red icon emphasis only, not a heavy red filled box.
- Template now has a real `Template` chrome header, matching the structural position used by the other panels.

The workspace geometry now avoids unusable squeezing:

- Batches minimum width: 240px.
- Document Viewer minimum width: 320px.
- Template minimum width: 420px.
- Resizers only render between visible panels.
- If a panel is closed or minimized, adjacent resizers disappear with it.

## 6. Template header simplification

The Template module was split into two clearer layers:

Panel chrome header:

- Left: `Template`
- Right: minimize, maximize/restore, close

Template content header:

- Export filename/title as the main title.
- Revision dropdown near the title.
- Export action on the right.
- Secondary context line for batch name and compact stats.

Secondary or duplicative header elements were reduced:

- Breadcrumb display hidden in the crowded header area.
- Export helper text hidden from the primary header.
- KPI/stats moved out of the title action row and into the context line.
- On narrower widths, title and actions wrap cleanly instead of overflowing.
- At constrained widths, secondary KPI details are suppressed rather than clipped.

The 1366px viewport E2E test initially caught real horizontal overflow in this header. CSS was tightened and re-run until the header passed at 1366x768, 1600x900, and 1920x1080.

Primary files:

- `webapp/frontend/src/components/TemplateWorkspace.tsx`
- `webapp/frontend/src/styles.css`
- `webapp/frontend/e2e/operator-visual.spec.ts`

## 7. Revision dropdown cleanup

The empty revision label no longer says `No runs`.

New behavior:

- If no saved revision exists, the dropdown shows `Current preview`.
- Empty popover text says the table is showing the current preview and no saved revisions exist yet.
- Saved revisions remain available through the same dropdown when present.

This keeps the current preview from looking like missing data.

## 8. Table polish

Table polish from the 2E/2F pass was preserved and reinforced:

- Slightly better left/right cell breathing room.
- Selected row uses fill-only styling, no border.
- Hover state is visible but restrained.
- Missing/invalid cell highlight remains cell-scoped.
- Unexplained yellow row dots remain removed.

No vendor extraction, export workbook, or template business logic was changed.

## 9. Screenshots

Screenshot directory:

`docs/reports/phases/screenshots/phase_2f_desktop_workspace_shell/`

Captured screenshots:

| File | Purpose |
|---|---|
| `01_normal_layout_all_panels.png` | Normal layout with all three panels visible and aligned. |
| `02_processing_running_stubbed.png` | Processing running state captured with safe route stubs. |
| `03_app_native_stop_confirm.png` | App-native stop confirmation dialog. |
| `04_cancelled_state_no_sticky_card.png` | Cancelled state after cancellation with no sticky cancelled card. |
| `05_one_panel_minimized_bottom_dock.png` | One panel minimized into the bottom dock. |
| `06_multiple_panels_minimized_dock.png` | Multiple panels minimized into the bottom dock. |
| `07_windows_menu_open.png` | Redesigned Windows menu open. |
| `08_template_maximized_focus_mode.png` | Template maximized/focus mode. |
| `09_template_restored.png` | Template restored to normal workspace. |
| `10_template_header_simplified.png` | Simplified Template header/chrome. |

Responsive E2E screenshots:

`docs/reports/phases/screenshots/phase_2f_desktop_workspace_shell/e2e/`

- `viewport_1920x1080.png`
- `viewport_1600x900.png`
- `viewport_1366x768.png`

## 10. Tests performed

Frontend:

```powershell
cd webapp\frontend
npm.cmd run build
npx.cmd tsc --noEmit
npm.cmd run test:e2e
```

Results:

- `npm.cmd run build` passed.
- `npx.cmd tsc --noEmit` passed.
- `npm.cmd run test:e2e` passed: 16 tests passed.

Backend:

```powershell
python -m compileall webapp\backend
python scripts\verify_backend_routes.py
```

Results:

- Backend compile passed.
- Route verifier passed.

Backend queue/cancel/revision smoke:

- Cancel queued job: passed (`removed_from_queue`).
- Cancel running job: passed (`cancelling`, then progress becomes `cancelled`).
- Confirm cancelled job does not create revision: passed.
- Completed fake job creates revision: passed.
- Idle cancel returns `no_active_run`: passed.

CLI compile regression checks:

```powershell
python -m py_compile "Training Bills_Invoices\Water - Sewer\Richmond Utilities\process_richmond_utilities.py"
python -m py_compile "Training Bills_Invoices\Water - Sewer\Hopkinsville Water Environment Authority\process_hopkinsville_water_environment_authority.py"
```

Results:

- Richmond Utilities CLI compile check passed.
- Hopkinsville Water CLI compile check passed.

Integrity:

- No AI calls made.
- No Dropbox upload triggered.
- No source PDFs/CSVs intentionally modified.
- `Output\Template.xlsx` was not intentionally modified.
- Vendor YAMLs were not modified.

## 11. Known limitations

- Processing-running and cancelled-state screenshots were captured with safe frontend route stubs to avoid vendor processors and Dropbox side effects. Backend queue/cancel/revision behavior was separately validated with a safe TestClient smoke.
- The old `CollapseRail` component still exists in source for now, but the active App shell no longer renders it. It can be deleted in a later cleanup after confirming no historical tests import it.
- The shell now has a coherent desktop model, but deeper keyboard accessibility for the Windows menu and dock can still be improved in a follow-up pass.
- If the user has stale localStorage width values from older phases, the new min-width guards prevent collapse, but a future reset-layout menu item would make recovery friendlier.

## 12. Next recommended phase

Phase 2G should focus on final desktop interaction refinement:

- Keyboard navigation and ARIA roles for Windows menu, dock items, and window controls.
- Persisted workspace layout presets: default, document-heavy, template-heavy, review mode.
- Clear reset-layout command.
- More complete manual QA on a long real Richmond/Hopkinsville run when Dropbox/export side effects are explicitly safe.
- Remove legacy shell components no longer used by the active App.
