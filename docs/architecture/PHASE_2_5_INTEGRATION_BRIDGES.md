# Phase 2.5 — Clean-checkout integration bridges

## Scope

Phase 2.5 connects the committed Phase 1 and Phase 2 contracts without copying
historical processor implementations. It does not change models, prompts,
provider routing, benchmarks, or learned-rule behavior.

## Stable bridges

- `RowAccountingV2Adapter` converts normalized rows into the existing V2 facts,
  semantic classification, candidates, and accounting decision flow.
- `AIResultAccountingV2Adapter` preserves extraction source fields and delegates
  each normalized row to the row adapter.
- `ServiceReasoningCandidateAdapter` exposes canonical service reasoning only as
  candidates. It cannot write the final GL.
- `ReadinessValidatedExporter` evaluates the exact normalized export snapshot
  and calls the workbook writer only when `export_allowed` is true.
- `is_payable_gl_account` is the shared catalog-membership/payability utility.

Only `AccountingDecisionEngine` selects `selected_gl`. Only
`AccountingReadiness` authorizes export.

## Integrated routes

```text
row_normalizer -> RowAccountingV2Adapter -> AccountingDecision -> readiness
ai_result_to_invoice -> AIResultAccountingV2Adapter -> AccountingDecision -> readiness
export request -> ReadinessValidatedExporter -> workbook writer
```

The opaque legacy workbook-copy route returns `legacy_export_disabled`; it is
retired and must not be restored. Historical vendor processors remain adapters
at the boundary and are not consolidated in this phase.

## Runtime assets

Production GL, property, vendor, template, and vendor-configuration assets are
external deployment inputs. Tests select a committed sanitized fixture root via
`INNER_VIEW_TEST_ASSET_ROOT`. A missing or ambiguous production asset root does
not silently substitute fixture-specific accounting evidence.

TK Elevator remains separated from the accounting regression assertion because
its unresolved property/location expectations depend on historical private
reference data. To promote it to a golden fixture, provide a sanitized source
document plus approved property and location labels. No GL or location value is
inferred to make that fixture pass.

`smoke_canonical_accounting_fixtures.py` is the Phase 2.5 gate for central GL
selection and source metadata. The older full canonical smoke remains a legacy
integration diagnostic: its vendor/property/generated-description mismatches
are reported separately and are not relabeled as processing or accounting
failures in shadow metrics.

## Compatibility and rollback

`ACCOUNTING_DECISION_ENGINE_V2` remains enabled by default. False-like values
are accepted only together with the explicit
`ACCOUNTING_DECISION_ENGINE_V2_ALLOW_LEGACY_ROLLBACK` authorization; missing or
ambiguous values cannot silently disable V2. Turning it off bypasses the
central decision integration and is an operational rollback with accounting
consistency risk; it requires an explicit release-owner decision. Remove the
flag after all supported runtime routes and sanitized golden fixtures have been
validated against V2 and the legacy adapter is retired.

## Acceptance boundary

This phase validates integration and regression fixtures; those fixtures are not
a model benchmark. Duplicate detection remains the separately tracked Phase 1
follow-up and is not synthesized here.
