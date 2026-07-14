# Phase 3 — AI/model routing baseline

## Outcome

Phase 3 introduces benchmark and orchestration contracts without changing
`AccountingReadiness` or the authority of `AccountingDecisionEngine`. Facts
extraction remains an upstream operation. Accounting reasoning receives typed
`DocumentFacts`; it cannot rewrite raw source text.

## Active contracts

- `ModelRegistry` declares configured roles: text extraction, vision extraction,
  and accounting reasoning.
- `CapabilityDiscovery` requires provider-advertised model identifiers. A key,
  configured name, or marketing label is not proof of availability.
- `RoutingStateMachine` produces deterministic reason codes for extraction and
  accounting shadow evaluation.
- `StrongAccountingReasonerShadow` returns a comparison with `applied=false`.
  It cannot mutate an `AccountingDecision`, row, readiness, or export state.
- `BudgetLedger` fails closed on cost or latency overruns.
- `VersionedJsonCache` uses different namespaces for facts and reasoning.
- `DecisionTrace` hashes document identifiers and excludes source text and raw
  provider payloads.

## State machine

```text
deterministic parser complete -> deterministic
image or weak OCR + vision capability -> AI vision extraction
incomplete facts + text capability -> AI text extraction
no extraction capability -> manual review

central AccountingDecisionEngine decision
  -> ambiguous + discovered strong model -> strong reasoning shadow
  -> otherwise -> central engine authoritative
```

The UI and export continue to consume backend `AccountingReadiness`. Neither
model confidence nor route selection can authorize export.

## GPT-5.6 status

GPT-5.6 was **not evaluated**. The current clean environment does not advertise
it through `AI_AVAILABLE_MODELS`, and no live provider probe or benchmark corpus
authorization is present. Setting `AI_ACCOUNTING_REASONING_MODEL=gpt-5.6` alone
does not make it available. If a provider later advertises the exact identifier,
it may run only in accounting shadow mode, subject to budget and sanitized
benchmark gates. No bulk migration is authorized.

## Budgets and caches

Cost and latency are pre-authorized and recorded per shadow evaluation. Budget
exhaustion fails before a call. Cache paths are structurally separated:

```text
cache/facts/<contract-version>/...
cache/accounting_reasoning/<contract-version>/...
```

Cache keys include namespace and contract version. Facts cache entries cannot
be read as accounting reasoning entries.

## Benchmark

The sanitized manifest is located under
`webapp/backend/tests/fixtures/document_benchmark`. Initial cases are synthetic
regression fixtures, not a model benchmark sufficient for promotion.

Metrics include field accuracy, GL fill/correctness/blank rates, review recall,
false Ready rate, route, processing latency, estimated cost, and failures.

```powershell
python scripts/run_document_reasoning_benchmark.py --sample 100 --model current
python scripts/run_document_reasoning_benchmark.py --sample 100 --model gpt-5.6
python scripts/compare_benchmark_results.py current.json candidate.json
```

The runner is dry-run by default and never implicitly contacts a provider. A
real evaluation requires an approved sanitized corpus and an injected executor
that keeps facts extraction and accounting reasoning results separate.

## Promotion policy

A strong model remains shadow-only until a representative benchmark establishes
all of the following against the current route:

- no increase in false Ready or accounting failures;
- improved GL correctness on ambiguous lines;
- budget compliance for cost and p95 latency;
- schema compliance and complete trace coverage;
- explicit human approval of the model identifier and registry entry.

Regression fixtures alone cannot satisfy this policy.

## Feature state and rollback

Phase 3 routing metadata is additive. With no discovered strong model, the
accounting route remains `central_engine_authoritative`. Rollback consists of
removing the additive orchestration call; it does not require changing Phase 1
or Phase 2 contracts. Ambiguous configuration fails unavailable, never enabled.

## Explicit exclusions

No self-training, automatic rule activation, Outlook integration, policy
marketplace, prompt replacement, provider migration, or production model call
was added.
