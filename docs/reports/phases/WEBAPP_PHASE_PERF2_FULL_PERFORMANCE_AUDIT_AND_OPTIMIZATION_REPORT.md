# Phase PERF-2 - Full Performance Audit and Fluidity Optimization

Date: 2026-05-21
Workspace: `<repository-root>`

## 1. Executive summary

PERF-2 completed a full performance pass across the frontend, backend batch APIs, processing instrumentation, document viewer, batch explorer, and QA suite. The work started with a separate audit report before optimization:

- `docs/reports/phases/PERF2_INITIAL_PERFORMANCE_AUDIT_REPORT.md`
- `docs/reports/phases/PROJECT_CLEANUP_AUDIT_REPORT.md`

The largest confirmed bottleneck was not one single expensive operation. It was repeated fan-out: batch switching triggered heavy metadata reads, file support detection, preview fetches, manual review fetches, revision fetches, and document rendering work that could overwrite newer UI state. The second largest bottleneck was render churn in large React surfaces: template grid, document viewer, and batch explorer.

PERF-2 added timing instrumentation, reduced heavy batch metadata work, cached safe preview/document operations, hardened stale frontend requests, memoized major surfaces, and updated the e2e suite to match the current combined-document viewer contract.

## 2. Audit findings

High severity:

- Batch list and batch switch APIs were doing too much synchronous metadata work.
- Frontend batch switching had stale request risk and too many request chains.
- Document viewer could rerender or jump from unrelated template state.
- Template grid rendered controlled editing surfaces too broadly.
- Batch explorer rendered too much detailed file/page state upfront.

Medium severity:

- Vendor detection repeatedly sampled and parsed the same files.
- Preview/manual-review/revision fetches were not consistently isolated.
- Progress/cancel telemetry existed but was too coarse for diagnosis.
- E2E tests had stale assumptions after the viewer changed to a combined page set.

Low severity:

- CSS remains oversized and should be split later.
- Some old generated screenshots and reports are still present by design.

## 3. Before/after timings

Measured baseline from audit:

| Flow | Baseline |
| --- | ---: |
| App open | 468 ms |
| Switch Alabama batch | 2729 ms |
| Template edit commit | 780 ms |
| Search/filter interaction | 425 ms |
| API calls during open + switch | 27 calls |
| `/api/batches` in stale/heavy path | about 3389 ms |
| `/api/batches/{id}` in stale/heavy path | about 2025 ms |

Current measured results after PERF-2:

| Flow | Current |
| --- | ---: |
| `/api/batches` with bounded summary warming | about 637-675 ms |
| `/api/batches/{id}` after removing heavy support detection | about 34.9 ms |
| Parallel preview/manual-review/revisions/performance fetch | about 388-489 ms |
| Pennyrile vendor detection dry-run | about 1851 ms -> 211-243 ms |
| McMinnville vendor detection dry-run | about 2538 ms -> 180-281 ms |
| Screenshot OCR PNG path | about 1086 ms |
| Screenshot OCR JPG path | about 861 ms |
| Warm OCR cache path | about 1.6-1.9 ms |

## 4. Frontend bottlenecks fixed

- Added dev-only performance marks in `webapp/frontend/src/perf.ts`.
- Added `AbortController` support in `webapp/frontend/src/api.ts`.
- Batch switching in `App.tsx` now cancels stale fetches and ignores stale responses.
- `DocumentPreviewPanel`, `TemplateWorkspace`, `ResManTemplatePreview`, `BatchExplorer`, and `BatchSelectorDropdown` were memoized.
- Search/filter inputs use deferred/debounced state where appropriate.
- Empty template grid now keeps a stable scroll container so layout tests and header behavior do not jump.
- The Single Invoice test path now waits for hydrated rows rather than clicking during preview load.
- Batch selector keeps the active batch available during asynchronous list refreshes.

## 5. Backend bottlenecks fixed

- Added `webapp/backend/services/perf_timer.py` v2 JSON/JSONL event recording.
- Added `GET /api/batches/{batch_id}/performance`.
- Added timing around preview build/cache, manual review build, validation, revision write, queue start, cancel request, and cancel settlement.
- Optimized `webapp/backend/api/batches.py` with light batch summaries, bounded cache warming, and lower-cost batch detail reads.
- Fixed `/files` tuple handling for CSV/Word source typing.
- Added page count support for file detail listing without forcing all heavy detection on batch switch.
- Optimized `vendor_detection.py` with text sample caching and fast keyword routing before legacy detectors.

## 6. Screenshot invoice processing analysis

`scripts/profile_screenshot_invoice.py` measured the image/screenshot path with external AI disabled:

- PNG OCR path: about 1086 ms.
- JPG OCR path: about 861 ms.
- Warm cache path: under 2 ms.
- Fake-ready assertion: passed. The profiler does not create fake ready rows.

Target under 5 seconds is met when OCR is enough. If vision is required, provider latency remains the hard lower bound, but the UI now records stage timing and remains responsive.

## 7. OCR, vision, and AI optimization

- Timing hooks now record AI/vision stages when called.
- Profilers run with `DROPBOX_DISABLE_FOR_TESTS=1` and `AI_FALLBACK_DISABLED=1`.
- The audit recommends keeping one vision call per page/file unless manually retried.
- Existing deterministic utility processors and canonical rules were not changed for output behavior during PERF-2.

## 8. Queue and cancel improvements

- Processing API now records queue submit/start timing.
- Cancel request, removed queued job, no-active-job, and settled states are recorded.
- Cancel flow can be inspected per batch through `/api/batches/{batch_id}/performance`.
- No final ready preview is intentionally created by the new instrumentation path after cancellation.

## 9. Document rendering improvements

- Document preview requests can now be aborted.
- Document preview cache preserves previous display while a newer preview loads.
- Stale preview responses are ignored.
- PDF Space-to-pan behavior was hardened so repeated Space keydowns do not trigger native scroll drift.
- Continuous viewer tests now validate the combined document set instead of the old selected-file-only page count.

## 10. API call optimization

- Batch switching avoids the old full file support detection path.
- Batch list summary caching reduces repeated filesystem scans.
- Preview/manual-review/revision/performance requests are parallelized where safe.
- Frontend stale request protection prevents older batch responses from overwriting newer selected batch state.
- Batch explorer file details are lazy and cached by batch/file state.

## 11. Cleanup audit summary

Cleanup report: `docs/reports/phases/PROJECT_CLEANUP_AUDIT_REPORT.md`

Performed safely:

- Verified ignored runtime areas: `.env`, `webapp_data/`, frontend build output, `node_modules`, `Output`, `Training Bills`, and `Old Scripts`.
- Removed test-created QA batches by exact QA/debug name pattern only.
- Did not delete source bills, training bills, backups, config files, or user data.

Deferred:

- CSS splitting and deeper style consolidation.
- Pruning old screenshot reports that may still be useful for visual regression history.
- Runtime batch pruning policy for real user batches.

## 12. Files changed

Primary PERF-2 files:

- `webapp/backend/services/perf_timer.py`
- `webapp/backend/api/batches.py`
- `webapp/backend/api/processing.py`
- `webapp/backend/services/vendor_detection.py`
- `webapp/frontend/src/perf.ts`
- `webapp/frontend/src/api.ts`
- `webapp/frontend/src/App.tsx`
- `webapp/frontend/src/components/BatchExplorer.tsx`
- `webapp/frontend/src/components/BatchSelectorDropdown.tsx`
- `webapp/frontend/src/components/DocumentPreviewPanel.tsx`
- `webapp/frontend/src/components/PopoutPage.tsx`
- `webapp/frontend/src/components/ResManTemplatePreview.tsx`
- `webapp/frontend/src/components/TemplateWorkspace.tsx`
- `webapp/frontend/src/components/pdf_workspace/PdfWorkspace.tsx`
- `webapp/frontend/e2e/operator-visual.spec.ts`
- `scripts/profile_processing_performance.py`
- `scripts/profile_frontend_api_flow.py`
- `scripts/profile_screenshot_invoice.py`

Reports/screenshots:

- `docs/reports/phases/PERF2_INITIAL_PERFORMANCE_AUDIT_REPORT.md`
- `docs/reports/phases/PROJECT_CLEANUP_AUDIT_REPORT.md`
- `docs/reports/phases/screenshots/phase_perf2/`

## 13. Tests performed

Frontend:

- `npm.cmd run build` - passed.
- `npx.cmd tsc --noEmit` - passed.
- `npm.cmd run test:e2e -- --workers=1` - passed: 36 passed, 1 skipped.

Backend and profiling:

- `python -m compileall webapp\backend` - passed.
- `python scripts\verify_backend_routes.py` - passed earlier in this phase.
- `python scripts\smoke_document_ingestion.py` - passed earlier in this phase.
- `python scripts\smoke_canonical_rules_engine.py` - passed earlier in this phase.
- `python scripts\smoke_canonical_invoice_fixtures.py` - passed earlier in this phase.
- `python scripts\smoke_utility_processors.py` - passed earlier in this phase.
- `python scripts\smoke_utility_e2e_outputs.py` - passed earlier in this phase.
- `python scripts\smoke_description_contract.py` - passed earlier in this phase.
- `python scripts\smoke_required_fields_contract.py` - passed earlier in this phase.
- `python scripts\smoke_ai_openai_compatible_provider.py` - passed earlier in this phase with no real AI call.
- `python scripts\smoke_ai_mapping_review.py` - passed earlier in this phase.
- `python scripts\profile_processing_performance.py` - completed and wrote timings.
- `python scripts\profile_screenshot_invoice.py` - completed and met OCR target.
- `python scripts\profile_frontend_api_flow.py` - completed against the PERF-2 QA server.

Environment note:

- Validation used updated backend `http://127.0.0.1:8002` and frontend `http://localhost:5175` because the requested `8001` process appeared stale/invisible during QA. The code changes are not tied to those ports.

## 14. Performance targets met or not met

Met:

- Real audit report created before/with fixes.
- Backend timings now recorded per batch.
- Batch switch request fan-out reduced and protected from stale writes.
- Document viewer no longer flickers from unrelated template state in tested paths.
- Screenshot OCR path is under 5 seconds when OCR is enough.
- Full frontend e2e suite is green in the controlled one-worker run.
- Backend compileall is green.
- No source bills, `.env`, `Output\Template.xlsx`, or `Old Scripts` were intentionally modified.
- Dropbox was disabled in automated tests.
- API keys were not exposed.

Partially met:

- Parallel e2e workers can still saturate the dev backend/browser stack. The reliable run is one worker. A production CI runner should either isolate backend state per worker or keep this suite serial.
- Full backend smoke suite was run earlier in the phase; after the final frontend-only patches, backend compileall was rerun.

## 15. Remaining limitations

- `styles.css` and `brand-refresh.css` remain large. Splitting them should be a separate visual-risk-managed phase.
- The batch list still depends on filesystem-backed `webapp_data`; for very large installations, a small SQLite/index layer would be cleaner.
- PDF rendering still depends on PDF.js worker latency for very large PDFs; caching mitigates but does not remove first-render cost.
- Vision provider latency cannot be made sub-5s if the provider itself is slow; the app can only expose accurate progress and cancellation.

## 16. Recommended next phase

Recommended Phase PERF-3:

- Introduce a durable batch/file index for `webapp_data`.
- Add production-mode Playwright perf budgets and traces.
- Split CSS into workspace, grid, document viewer, and batch explorer modules.
- Add a small React render-count harness for template grid and document viewer.
- Add CI worker isolation or force serial e2e execution for stateful browser tests.
