# Phase 3.8 — Economic Responsibility, Reimbursement and Allocation

## Scope

This phase adds a typed, deterministic responsibility model before Reviewer 1
labeling. It does not change model routing, `AccountingDecisionEngine`, or
`AccountingReadiness`.

The following remain separate decisions:

```text
document identity
payment source
economic bearer
settlement treatment
allocation scope and targets
line-level responsibility
GL selection
readiness/export authorization
```

GL does not determine responsibility. Shipping address, billed entity, vendor
type, subscription status, handwriting, filename, and folder context are
evidence—not automatic authority.

## Contracts

- `PaymentSourceType`
- `EconomicBearerType`
- `SettlementTreatment`
- `AllocationScope`
- `ResponsibilityEvidence`
- `AllocationTarget`
- `LineResponsibility`
- `EconomicResponsibility`

Contract version: `economic-responsibility/1.0`.

Allocation percentages must total 100 and target references must be unique.
Mixed document treatment requires explicit line-level decisions.

## Conservative classification

The deterministic classifier consumes typed evidence claims. Evidence strength
and confidence are scored; close conflicts resolve to `unknown` and manual
review. Reimbursement is produced only when evidence independently establishes:

1. management-company payment source;
2. property economic bearer; and
3. single-property allocation.

Property shipping alone does not establish who paid. Management-company billing
alone does not establish corporate economic responsibility. Insufficient or
contradictory evidence produces review.

Strong reasoning, candidate profiles, learned rules, and automatic activation
are not used.

## Filename and folder context

`FilenameFolderContextParser` creates versioned observable facts and generic
metadata candidates for amounts, dates, unit/project tokens, and expense
categories. Candidates are always marked `authoritative=false` and are converted
only to weak evidence.

Original filename and folder values remain inside the private workspace. Blind
payloads expose parsed candidates, never the original filename or private path.
Filename candidates never overwrite document source text, normalized document
text, or generated descriptions.

## Reviewer 1 schema

Reviewer 1 labels now use `reviewer-1-label/2.0` and contain document- and
line-level economic responsibility fields. Unknown values remain explicit and
must carry a reason. Allocation and responsibility evidence use lists with page
and region references where available.

One pre-existing invalid draft was migrated privately from schema 1.0 to 2.0:

- original preserved in a private `_schema_v1_backup` directory;
- audit event records the prior schema version;
- responsibility values initialized as explicit unknown;
- completion remains in-progress and validation remains invalid;
- no Reviewer 1 answer was inferred or completed automatically.

The frozen dataset remains `selected_120_v1` with SHA-256
`8b5c065d8898a7aa32e56a150bc1cdf2f2a10599005f901000385313090ffcbf`.

## Safety invariants

- No reimbursement without sufficient combined evidence.
- Document- and line-level responsibility may differ.
- Mixed treatment is explicit.
- Filename/folder and handwritten metadata remain evidence.
- Responsibility classification has no GL input.
- GL remains selected only by `AccountingDecisionEngine`.
- Export remains authorized only by `AccountingReadiness`.
- No private label, filename, path, vendor, address, or note is committed.
