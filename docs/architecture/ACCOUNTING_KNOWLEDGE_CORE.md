# Accounting Knowledge Core

Version: `accounting-knowledge-core/1.0`

The Accounting Knowledge Core is the tenant-isolated integration boundary
between Cross-Report Analytics, inline Human Adjudication and future invoice
processing. It aggregates evidence without merging authority levels.

## Stores and authority

| Store | Source | Mutability | Production effect |
|---|---|---|---|
| `HistoricalProfile` | Posted ResMan report snapshots | Immutable snapshot | Weighted GL candidate evidence only |
| `HumanCorrectionLedger` | Invoice Processor corrections | Append-only revisions | Exact invoice overlay |
| `BenchmarkExample` | Approved evidence-backed correction | Immutable/versioned | Evaluation only; never queried for production candidates |
| `ApprovedLearningExample` | Separately approved tenant correction | Append-only governance event | Similar candidate evidence only |
| `GovernedRule` | Simulated and Controller-approved tenant policy | Versioned | Constrains/prioritizes candidates only |
| `FinalAccountingEvent` | Successful readiness-authorized export or later explicit posting acknowledgement | Append-only/idempotent | Analytics only |

`AccountingDecisionEngine` remains the only selected-GL authority.
`AccountingReadiness` remains the only export authority.

## Processing flow

```text
immutable document evidence
        + immutable HistoricalProfile snapshot
        + tenant-private ApprovedLearningExample
        + active simulated GovernedRule
        -> Accounting Knowledge line context
        -> candidate evidence / constraints
        -> AccountingDecisionEngine
        -> AccountingReadiness
```

Benchmark examples are intentionally absent from the production half of this
flow. Human corrections do not rewrite imported ResMan history or DocumentFacts.
Stale Cross-Report snapshots are reported but excluded from production candidate
evidence. Missing history is neutral and does not penalize a novel vendor.

Generated accounting and semantic cache identities include the tenant, GL
catalog, tenant policies, human correction/learning ledger, governance events,
historical snapshot, canonical ontology and configured model/profile
fingerprints. Cache artifacts are derived data and never source evidence.

## Promotion workflow

```text
observed pattern
-> repeated corrections
-> approved learning pattern
-> simulated rule
-> authorized rule approval
-> deterministic candidate constraint
```

There is no automatic promotion. Bulk optional scopes require an explicit
human confirmation and equivalent learning/rule proposals are deduplicated by
canonical semantic context. Eligibility signals use configurable
`KNOWLEDGE_MIN_REPEATED_CORRECTIONS`, `KNOWLEDGE_MIN_DISTINCT_INVOICES` and
`KNOWLEDGE_MIN_LEARNING_FOR_RULE` thresholds; crossing a threshold only creates
a review signal. Simulation and authorized approval remain mandatory.
Platform-wide promotion is outside this contract.

## APIs

- `GET /api/knowledge-core/batches/{batch_id}/lines/{row_index}` returns the
  evidence, three historical prior dimensions, similar approved learning,
  matching active rules, contradictions, confidence and provenance.
- `POST /api/knowledge-core/impact` estimates the independently selected save
  scopes before confirmation.
- `GET /api/knowledge-core/analytics` returns Cross-Report distributions,
  disagreement, approval counts, rule coverage and correction drift.

All endpoints derive tenant identity server-side. No browser-provided tenant can
cross the runtime tenant boundary.

## Export and posting lifecycle

`approved_export` is appended only when a complete versioned
`AccountingReadiness` payload has `export_allowed=true`, and only after the
workbook writer succeeds. Retrying the same tenant, batch, row, GL and readiness
snapshot is idempotent. It means exported/approved, not posted in ResMan.

`posted` is a distinct future event that may be appended only after an explicit
ResMan acknowledgement. The analytics API therefore exposes approved-export and
posted distributions separately. Canceled or superseded exports do not become
posted history merely because a workbook was generated.

Rule creation, simulation and approval are three distinct operations. The
human-adjudication approval endpoint refuses a draft without a current tenant
policy simulation; it never creates a synthetic simulation during approval.
