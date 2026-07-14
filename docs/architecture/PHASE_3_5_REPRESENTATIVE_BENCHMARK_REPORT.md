# Phase 3.5 — Representative Accounting Benchmark Report

## Executive outcome

The representative benchmark framework, labeling contract, double-review
workflow, privacy boundary, current-profile runner, candidate capability gate,
metrics, and cohort analysis are implemented and reproducible.

The representative dataset acceptance criterion is **not met**. The environment
did not provide `INNER_VIEW_PRIVATE_BENCHMARK_ROOT`; only two synthetic,
public-safe gold fixtures are available. No private documents were copied, no
labels were invented, and synthetic variants were not counted as real documents.

Recommendation: **collect more data**. Do not promote a model, start a candidate
shadow comparison, or begin a canary from this sample.

## Dataset status

| Status | Count |
|---|---:|
| Gold, double-reviewed and adjudicated | 2 |
| Partial | 0 |
| Unlabeled | 0 |
| Required representative gold documents | 100 |

Collection targets are versioned in `collection_plan.json`: ten documents from
each required class—digital PDFs, utilities, contractors, materials,
fees/renewals/subscriptions, scans, photos, handwriting,
legal/insurance/finance, and unknown documents.

The two committed cases cover only clean digital service and synthetic material
text. They are framework regression fixtures, not representative evidence.

## Privacy and provisioning

- Private documents must live outside Git under
  `INNER_VIEW_PRIVATE_BENCHMARK_ROOT`.
- Manifest references use safe relative `private/...` paths.
- Absolute paths and traversal references are rejected.
- Git contains only sanitized labels and synthetic/public-safe fixtures.
- Reviewer-facing values use redacted vendor, property, invoice number,
  description, and location fields.
- Results contain case identifiers and metrics, not raw document text.

## Labeling and adjudication

The schema records document family, redacted vendor/property/invoice fields,
dates, totals, line items, amounts, location, line/trade/work semantics, expected
GL, acceptable alternatives, review/block expectations, author, confidence, and
adjudication state.

Gold requires:

1. First reviewer label.
2. Independent second reviewer label.
3. Conflict marking when applicable.
4. Explicit adjudicated gold label.

Gold validation fails closed when any required review stage is absent. Partial
and unlabeled cases are stored separately and excluded from scoring.

## Current profile execution

Execution date: 2026-07-14. Profile: `current`. Documents scored: 2 synthetic.

| Metric | Result |
|---|---:|
| Field accuracy: invoice number | 1.00 |
| Field accuracy: amount | 1.00 |
| Semantic line/trade/work accuracy | 1.00 |
| GL top-1 | 1.00 |
| GL top-3 | 1.00 |
| False Ready | 0.00 |
| False Block | 0.00 |
| Review precision | 1.00 |
| Review recall | 1.00 |
| Route accuracy | 1.00 |
| AI calls | 0 |
| Latency p50 | 4 ms |
| Latency p95 | 134 ms |
| Cost per document | USD 0.00 |
| Cost per successful GL | USD 0.00 |
| High-confidence errors | 0 |

These figures only prove deterministic framework behavior on two tiny synthetic
documents. They must not be used as product-quality or model-selection claims.

## Cohort analysis

Available cohorts:

- digital: 1;
- materials: 1;
- service: 1;
- unknown vendor: 2;
- simple invoice: 2;
- deterministic route: 2;
- normal value: 2.

Missing cohorts prevent representative analysis: scans, photos, handwriting,
utilities, known vendors, mixed invoices, high-value invoices,
legal/insurance/finance, subscriptions/renewals, and unknown documents.

## High-confidence errors

None were observed in the two synthetic cases. The runner lists every future
high-confidence top-1 GL error individually with case id, confidence, expected
GLs, and ranked GLs. An empty list here is not evidence that the production
error rate is zero.

## Routing, models, cost, and shadow policy

The current cases used deterministic routing and made zero AI calls. Candidate
profiles execute only after `CapabilityDiscovery` confirms the exact model id.

`gpt-5.6` result: `SKIPPED — capability_not_discovered`.

Strong accounting reasoning remains shadow-only. No profile was promoted and no
production route or model was activated.

## Reproduction

```powershell
$env:INNER_VIEW_PRIVATE_BENCHMARK_ROOT = "D:\approved-private-benchmark"
python scripts/audit_representative_benchmark_dataset.py --require-minimum 100
python scripts/run_representative_accounting_benchmark.py --profile current --output docs/architecture/PHASE_3_5_CURRENT_RESULTS.json
python scripts/run_representative_accounting_benchmark.py --profile <candidate-model-id>
```

The dataset audit intentionally exits nonzero until 100 gold cases exist and all
referenced documents and labels are available.

## Acceptance criteria

| Criterion | Status |
|---|---|
| At least 100 labeled real documents | **FAIL — 2 synthetic gold only** |
| Gold/partial/unlabeled separated | PASS |
| False Ready explicitly reported | PASS |
| High-confidence errors individually listed | PASS |
| Routing and cost measured | PASS for available cases |
| Reproducible benchmark | PASS |
| No private data committed | PASS |
| Strong model remains shadow-only | PASS |
| No push | PASS |

## Next decision

Remain on the current profile and collect/adjudicate at least 98 additional
approved documents while satisfying every cohort target. Once the dataset audit
passes, rerun `current`. A candidate may then be considered for **shadow only**
if capability discovery confirms it. Limited canary is not currently justified.
