# Phase 2 stabilization baseline

Baseline date: 2026-07-14. This checkpoint freezes the contracts and quality
gates after Phase 2. It does not authorize Phase 3 work, model changes, prompt
changes, routing changes, or automatic activation of learned rules.

## Active contracts and versions

| Authority | Active version | Role |
| --- | --- | --- |
| `DocumentFacts` / `LineItemFacts` | `document-facts/1.0` | Immutable observed and normalized document evidence |
| `SemanticClassification` | `semantic-classification/1.0` | Deterministic line semantics before GL selection |
| GL catalog | `gl-catalog/1.0` | Versioned chart membership and payable metadata |
| `AccountingDecisionEngine` | `accounting-decision/1.0` | Sole selector of `selected_gl_code` |
| `AccountingReadiness` | `accounting-readiness/1.0` | Sole readiness and export authorization authority |

`AccountingReadiness` was not replaced by Phase 2. The decision engine supplies
the selected GL; readiness independently validates the final export row and
continues to own `export_allowed`.

## Integrated routes

- `batch_processor` sends cached preview/export rows through the shared
  `row_normalizer` boundary.
- `row_normalizer` captures source fields before display normalization and runs
  the Phase 2 adapter for every row.
- `ai_invoice_processor.ai_result_to_invoice` runs the same adapter for direct
  callers that do not traverse the batch boundary.
- The service-invoice reasoner now emits typed candidates and delegates final
  selection to `AccountingDecisionEngine`.
- Bulk, Single Invoice, Billing V2, and export retain the Phase 1
  `AccountingReadiness.export_allowed` gate.
- `GlAccountExplanation` displays backend decision evidence and versions; it
  does not reconstruct accounting reasoning in the browser.

## Feature flags and rollback

`ACCOUNTING_DECISION_ENGINE_V2` is enabled by default. Missing or blank values
mean enabled. Explicit true values are `1`, `true`, `on`, and `yes`. Any
ambiguous value aborts instead of silently choosing a path.

The flag exists only as an emergency rollback during the shadow/stabilization
window. Disabling it requires both:

```text
ACCOUNTING_DECISION_ENGINE_V2=false
ACCOUNTING_DECISION_ENGINE_V2_ALLOW_LEGACY_ROLLBACK=true
```

Only the production release owner or incident commander may authorize that
pair, after recording an incident/change ticket. Turning it off preserves V2
facts, semantics, decisions, and shadow provenance but leaves the provisional
legacy GL in the compatibility row. This can reintroduce divergent accounting
selection and is therefore unsafe as a normal operating mode. Readiness still
gates export, but it cannot guarantee that a valid legacy GL is semantically
correct.

Retire both flags after the approved production comparison corpus has no
unexplained material differences for the agreed observation window and all
remaining GL writers have explicit candidate adapters. The five canonical
fixtures and nine lines in this checkpoint are regression coverage only, not a
model benchmark or sufficient evidence to retire rollback.

## Temporary legacy surface

- Existing processors may populate provisional `GL Account`; the shared
  adapter converts it to `GLCandidate`, records shadow provenance, and V2
  overwrites the final value when enabled.
- Source-specific producers still include deterministic parser, canonical
  rule, utility rule, AI candidate, vendor default, historical mapping,
  approved learned correction, service reasoner, and manual approved rule.
- `ai_gl_accounting_reasoning` temporarily mirrors `accounting_decision` for
  older API/UI consumers.
- The row-shaped `GL Account` column remains a compatibility projection of
  `selected_gl_code`.
- Legacy workbook copying is retired and must not be restored.
- Duplicate status remains a readiness contract input pending integration with
  the real duplicate detector; that work is tracked separately.

These adapters are compatibility infrastructure, not independent accounting
authorities. Learned rules are not activated automatically.

## Supported canonical fixtures and shadow summary

Complete regression fixtures: Capital Waste, EPB, Lowe's Pro Supply, Spectrum,
and TK Elevator. Their persisted deterministic summary is in
`PHASE_2_SHADOW_METRICS.json` and can be reproduced with:

```powershell
python scripts\smoke_phase2_shadow_metrics.py
```

Checkpoint metrics: 5 invoices, 9 lines, 9 legacy/V2 equal, 0 different,
0 blocked decisions, 0 missing GL, 5 semantic-unknown lines, and 0 processing
failures. Semantic unknown is intentionally visible rather than inferred from
AI confidence.

ServAll remains `SKIPPED`. The only available evidence is a low-resolution
conversation screenshot. It does not reliably establish invoice number,
invoice date, due date, property, total, exact line amounts, or which legal
vendor entity issued the document (Clarksville Termite & Pest Control versus
Servall LLC Martin). Converting it to a golden fixture requires the original
invoice/service ticket at readable resolution, or a stable saved OCR/vision
candidate payload reviewed against that original, plus labels for exact vendor
entity, invoice identifiers/dates, property, total, and line-level amounts and
descriptions. No values may be invented from the placeholder.

## Worktree isolation map

The checkpoint does not reset, stage, delete, or rewrite user changes. Because
the repository already had a large uncommitted history, isolation is by
responsibility and path:

1. **Phase 1:** `accounting_readiness.py`, readiness API/export integration,
   Billing V2 backend/frontend contracts, `test_accounting_readiness.py`,
   `billing-v2.spec.ts`, and readiness-related changes in shared API/UI types.
2. **Phase 1 hardening:** README/requirements test setup, legacy workbook-copy
   retirement documentation/tests, `readiness-gate.spec.ts`, and the separately
   tracked duplicate-detector integration issue.
3. **Phase 2:** `accounting_contracts.py`, `semantic_classifier.py`,
   `gl_catalog.py`, `accounting_decision_engine.py`,
   `accounting_pipeline_v2.py`, `service_invoice_gl_reasoning.py`,
   `accounting_decision_v2.yaml`, `test_accounting_decision_v2.py`,
   `GlAccountExplanation.tsx`, the source-preservation integration in
   `row_normalizer.py`/`ai_invoice_processor.py`, and this architecture baseline.
4. **Prior unrelated changes:** vendor/utility processors, AI ingestion/provider
   work, canonical-rule changes, `config/vendors/**`, Punctual Process business
   data and builder, performance/layout work, PDF workspace/UI redesign,
   vendor reports, screenshots, and their smoke/profile scripts. These are not
   claimed or altered by this checkpoint.
5. **Generated/runtime:** root `tmp_*.png`, Python caches/coverage, frontend
   `dist`, Playwright `test-results`/report, `webapp_data`, processing logs,
   manual-review workbooks, and ResMan import outputs. Standard reproducible
   runtime paths are ignored. Report screenshots and Punctual Process outputs
   remain visible because they may be intentional user evidence/deliverables
   and must not be deleted or hidden without owner review.

`webapp/frontend/test-results/.last-run.json` is already tracked historically.
The new ignore rule prevents new result files after it is intentionally removed
from the index, but this checkpoint does not untrack or revert it.

Several shared files contain changes from more than one group. Their ownership
must be split by hunk during a future intentional staging/commit operation;
file-level staging would mix phases.

## Reproducible quality gates

Run from the repository root unless noted:

```powershell
python -m compileall -q webapp/backend
python -m pytest --collect-only -q webapp/backend/tests
python -m pytest -q webapp/backend/tests
python scripts\smoke_required_fields_contract.py
python scripts\smoke_canonical_invoice_fixtures.py
python scripts\smoke_phase2_shadow_metrics.py
Set-Location webapp/frontend
npm.cmd run build
npx.cmd playwright test e2e/readiness-gate.spec.ts --project=chromium
```

Checkpoint results are recorded after the final run below; a green historical
record does not replace rerunning these commands on the target revision.

| Gate | Checkpoint result |
| --- | --- |
| Backend compile | PASS |
| Pytest discovery/full backend suite | PASS: 43 collected, 43 passed, 4 subtests passed |
| Unittest discovery | PASS: 7 discovered and passed |
| Accounting readiness | PASS: 7 tests, 4 subtests |
| Accounting Decision V2 | PASS: 32 tests |
| Required-fields smoke | PASS |
| Service reasoning | PASS: 4 tests |
| Canonical fixtures | PASS: 5 complete; ServAll explicitly skipped |
| Shadow metrics | PASS: 5 invoices / 9 lines / 0 failures |
| Frontend build | PASS: TypeScript and Vite production build |
| Readiness Playwright | PASS: 2 tests on Chromium |
| Global `git diff --check` | PASS |
