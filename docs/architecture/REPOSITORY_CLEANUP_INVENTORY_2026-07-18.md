# Repository Cleanup Inventory — 2026-07-18

This inventory was created on `chore/repository-cleanup-2026-07-18` after the
preservation snapshot `f5c85801cf715f8411d6445d7a026cdd9b243b98` was verified
on the remote. Cleanup must not change accounting, readiness, extraction, or
tenant-isolation behavior.

## Approved low-risk cleanup groups

| Path | Evidence | Replacement / preservation | Risk | Validation |
|---|---|---|---|---|
| `docs/reports/phases/screenshots/` | 134 tracked PNG/JPG artifacts (23,276,378 bytes); no production import or build reference; the directory is now ignored | Remove from Git tracking only. Local copies remain untouched and the complete tree remains recoverable from the snapshot branch/tag | Low | `git diff --check`; active E2E; verify local files still exist |
| `webapp/frontend/e2e/operator-visual.spec.ts` | Targets substantial retired-shell UI and old selectors; still valuable as historical coverage | Keep the file, but run only through a dedicated legacy Playwright config | Medium | Active suite must remain green; legacy status stays documented |
| `webapp/frontend/e2e/utility-u4.spec.ts` | Historical utility visual QA with private/runtime batch assumptions | Keep the file in the legacy suite | Medium | Active suite must remain green |
| `webapp/frontend/e2e/ingestion-ai9.spec.ts` | Historical ingestion-shell workflow and generated screenshot expectations | Keep the file in the legacy suite | Medium | Active suite must remain green |
| `webapp/frontend/e2e/reviewer-assisted-workspace.spec.ts` | Reviewer workspace is not the primary Billing V2 adjudication workflow | Keep the file in the legacy suite | Medium | Active human-adjudication E2E must remain green |
| `webapp/frontend/playwright.config.ts` | Default discovery currently mixes active and retired-shell specs | Make the default gate active-only using explicit legacy exclusions | Low | `npm run test:e2e:active` |
| `webapp/frontend/playwright.legacy.config.ts` | No separate runner currently preserves historical specs | Add an explicit non-release-gate runner | Low | Playwright discovery only; failures remain documented, not hidden |
| `webapp/frontend/package.json` | Scripts do not distinguish active from legacy E2E | Add named active and legacy commands | Low | Frontend build and active E2E |

## Preserved high-risk or unknown candidates

| Path / group | Why not removed | Reference evidence / protection |
|---|---|---|
| `webapp/frontend/src/App.tsx` legacy batch shell | Still contains adapters, document viewer plumbing, and assistant integration; removal is high-risk | Referenced by the active frontend entry point and historical E2E |
| `webapp/backend/api/` compatibility adapters | Callers and runtime routes remain mixed across Billing V2 and legacy clients | Registered routers and the 432-test backend suite |
| `webapp/backend/services/` extraction/cache adapters | Apparent duplication includes compatibility and invalidation boundaries | Imports, processing routes, accounting tests, and benchmark replay tests |
| `scripts/` benchmark, smoke, and migration utilities | Unreferenced-by-import is not proof of obsolescence for operator scripts | Documentation references and manual operational use; preserve pending owner review |
| `webapp/frontend/src/styles.css` and `brand-refresh.css` | Large and overlapping, but visual consolidation has high regression risk | Production build plus active and legacy visual coverage |
| Legacy E2E files listed above | Failing retired selectors do not make the tests disposable | Preserved in a separately named suite and documented status |

## Cleanup boundary

No production source, accounting policy, model/provider routing, parser,
fixture expectation, worktree, private runtime file, or user-authored source is
deleted in this cleanup. Unknown and high-risk candidates remain preserved for
manual review.
