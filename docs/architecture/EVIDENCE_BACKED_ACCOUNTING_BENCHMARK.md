# Evidence-backed accounting benchmark

## Status

This contract separates deterministic replay from independent extraction
accuracy. It applies to the seven-invoice, 104-payable-row latency batch used
for the current optimization work.

- Contract: `evidence-backed-accounting-golden/1.0`
- Canonical semantics: `canonical-line-concepts/1.0`
- Review taxonomy: `accounting-review-taxonomy/1.0`
- Gate contract: `evidence-benchmark-gates/1.0`
- Deterministic replay gate: PASS
- Independent extraction gate: `blocked_pending_human_adjudication`

The independent gate is intentionally not green. A model verifier is not a
human adjudicator, and no previous model run is accepted as ground truth.

## Privacy boundary

Original documents, rendered pages, crops, literal source text, verifier
responses, and the pending adjudication workspace remain under the ignored
private runtime directory. Git contains only contracts, workflow code,
synthetic tests, and aggregate results. Evidence references use relative paths
and SHA-256 hashes; absolute paths are rejected by validation.

## Ground-truth workflow

Each field has three distinct layers:

1. extractor candidates from the independently observed cold run and the prior
   accepted run;
2. targeted verifier observations linked to an exact source hash, page, crop
   coordinates, and crop hash;
3. a human adjudication containing reviewer, timestamp, accepted value,
   acceptable alternatives, and rationale.

Only layer 3 can set `state=adjudicated`. The Pydantic contract rejects a gold
field without human adjudication and source evidence.

## Targeted visual adjudication

The private workspace contains all 338 historical critical differences. Of
these, 233 are visual-field events. Equivalent events are deduplicated into 46
field-specific crops and verifier requests. Identity, component, amount, and
PAID evidence use separate crops so an adjacent row cannot contaminate the
observation.

The high-priority Window Sill versus Tub Mat disagreement and disputed
handwritten identities remain pending human confirmation when the verifier
cannot read the pixels confidently. Catalog membership is never supplied to
or used by the visual verifier.

## Canonical semantic ontology

Raw source wording remains immutable. A deterministic, vendor-neutral layer
maps equivalent wording to stable concepts such as countertop refinishing,
bathtub refinishing, wall-tile refinishing, window-sill refinishing, tub-mat
work, and key-by-code service.

Semantic candidate cache identity now uses:

- canonical concept;
- work mode and semantic family;
- allowed candidate set;
- tenant accounting dependency fingerprint;
- provider, profile, model, and reasoning version.

Literal description, amount, and invoice prose no longer define cache
identity. Unresolved concepts are not reusable cache keys. Cache contents are
candidate-only; `AccountingDecisionEngine` remains the only GL authority.

## Typed review taxonomy

Historical `ai_warning_<free text>` identities are migrated to stable typed
categories while the original warning text remains explanatory evidence.
Examples include `handwritten_date_ambiguous`, `row_identity_ambiguous`,
`visual_component_conflict`, `property_unresolved`,
`total_reconciliation_failed`, `paid_marker_ambiguous`, and
`payment_terms_conflict`.

The offline replay contains zero remaining free-text-derived warning codes.
No readiness or export policy changed; `AccountingReadiness` remains the only
export authority.

## Offline results

- Invoices: 7
- Payable rows: 104
- Excluded PAID rows represented separately: 4
- Exact source/crop evidence losses: 0
- Canonical concepts resolved: 103 payable rows
- Unresolved payable concepts without a blocker: 0
- External calls during replay: 0
- False-safe exports: 0
- Invoices blocked by AccountingReadiness: 7
- Unauthorized GL outcomes: 0
- Exact repeated downstream replay parity: PASS

There are 488 human-adjudicable field records. None is presented as human gold
until a reviewer completes the private adjudication workspace. Therefore a
new full cold provider benchmark remains prohibited.

## Reproduction

The private operator runs these scripts against an ignored benchmark root:

```text
python scripts/build_evidence_backed_golden.py --base <saved-cold-root> --output <private-benchmark-root>
python scripts/verify_evidence_disagreements.py --benchmark-root <private-benchmark-root>
python scripts/replay_saved_cold_facts.py <batch-id> --runtime-root <saved-runtime-root> --result <result.json> --metrics <metrics.json>
python scripts/evaluate_evidence_benchmark_gates.py --benchmark-root <private-benchmark-root>
```

The full cold extraction benchmark may run only after deterministic replay
remains green and the independent evidence contract has been human-adjudicated.
