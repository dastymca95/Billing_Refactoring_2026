# Accounting Decision Engine V2 migration

## Authority boundaries

The Phase 2 pipeline separates six stages:

1. `DocumentFacts` stores observed values and immutable source evidence.
2. `SemanticClassification` classifies every line, including `unknown`.
3. Legacy producers are converted to `GLCandidate` objects.
4. `AccountingDecisionEngine` is the only component that sets `selected_gl_code`.
5. The row adapter copies that selected value into the legacy `GL Account` column.
6. `AccountingReadiness` remains the only export authorization authority.

Raw source text, normalized source text, and generated accounting descriptions
are separate fields. Generated descriptions are never accepted as sole
reasoning evidence.

## Temporary adapters

`accounting_pipeline_v2.py` adapts processor output at the shared row
normalization boundary. Existing processors may still emit a provisional
`GL Account`; the adapter captures it as a candidate, records its source, and
then replaces it only with the engine's selected result. This provides one
final accounting truth without rewriting every parser in one release.

`ai_gl_accounting_reasoning` temporarily mirrors `accounting_decision` for old
API consumers. New UI code reads `accounting_decision` directly.

## Feature flag and shadow comparison

`ACCOUNTING_DECISION_ENGINE_V2` defaults to enabled. Setting it to `0`,
`false`, `off`, or `no` leaves the legacy GL column unchanged for emergency
diagnosis, while V2 contracts and tests remain active. It must not become a
second permanent production path.

Every adapted row records:

```json
{
  "legacy_selected_gl": "...",
  "v2_selected_gl": "...",
  "same": true,
  "difference_reason": null
}
```

Shadow data is provenance only and never authorizes export.

## Retirement plan

1. Measure shadow differences across the canonical and approved gold corpus.
2. Convert remaining processor writes into explicit source-specific candidate
   adapters (`utility_rule`, `canonical_rule`, `manual_approved_rule`, etc.).
3. Remove processor writes to `GL Account` after their adapters have coverage.
4. Remove the `ai_gl_accounting_reasoning` compatibility mirror.
5. Remove the feature-flagged legacy branch after benchmark approval.

The retired legacy workbook-copy export path must not be restored.
