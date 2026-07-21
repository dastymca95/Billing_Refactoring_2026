# Phase A supplementary transport V2

Status: offline implementation only. No private provider request was executed.

## Authority boundary

`GeminiSupplementaryTransportV2` transports observable visual evidence only.
It contains no GL, accounting policy, readiness, export, benchmark, learning,
human-correction, or governed-rule field. A valid transport still has to pass
strict `GeminiSupplementaryObservation` validation, planned-crop validation,
merge, reconciliation, `AccountingDecisionEngine`, and
`AccountingReadiness`, in that order.

## V1 and V2

| Contract | Shape | Status |
| --- | --- | --- |
| `supplementary-transport/1.x` | `{ "payload_json": "<JSON string>" }` | Historical telemetry reader only. Never used to construct a new request. |
| `supplementary-transport/2.0` | Direct typed fields | Required for every future private supplementary request. |

Historical V1 values are decoded exactly once and revalidated using the old
compatibility adapter. Existing runs are not rewritten or reinterpreted.

## Direct V2 structure

Top-level required fields:

- `contract_version`
- `target_type`
- `visibility_status`
- `unresolved_flag`
- `contradiction_flag`
- `page_number`
- `confidence`
- `raw_visible_text`
- `observed_candidate_value`
- `observed_candidates`
- `financial_components`
- `warnings`

There is no active top-level provider `evidence_references` field. Evidence is
attached as `evidence_refs` to each raw-text observation, primary candidate,
identity candidate, financial component, visible label, and contradiction.

The primary observed value is one bounded object with a resolution kind,
field/value, optional flat line-item fields, planned crop ID, confidence, and
visibility. Identity alternatives, financial components, and evidence
references are arrays of bounded objects. Candidate types, component types,
evidence kinds, target types, visibility states, and resolution kinds are
enums. Crop IDs and roles are request-local enums generated from the validated
evidence plan.

Every object declares explicit properties and rejects additional properties.
The schema contains no `oneOf`, `anyOf`, recursive reference, free-form
dictionary, or JSON encoded inside a string. Flash-Lite and Gemini 3 Flash
Preview use the same semantic schema; only their native HTTP/model settings
may differ.

The privacy-free audit fixture reports depth 5, 44 bounded properties, 44
required fields, five object schemas with `additionalProperties = false`, zero
unsupported keywords, zero object/array nullable conflicts, and medium
complexity risk. Unknown primary observations use the explicit bounded `none`
object instead of a nullable object union.

Privacy-free V2 semantic-family SHA-256:

`6bba9a5e73c7ebf6d6b6cba65620b02154d6aad408e4dc090926a6fcd5bc98cd`

Each concrete request also receives a separate schema fingerprint because its
target and approved crop-ID/role enums are request-specific.

## Unknown and null mapping

| Internal meaning | V2 representation |
| --- | --- |
| No visible primary candidate | Bounded primary object with `resolution_kind = "none"`, null optional values, no crop, and `visibility_status = "not_visible"`; local normalization maps it to the internal `null` candidate |
| No identity alternatives | `observed_candidates = []` |
| No visible financial components | `financial_components = []` |
| No valid evidence reference | the owning observation uses `evidence_refs = []`; visible or ambiguous values then fail evidence validation |
| Page not visually established | `page_number = null` |
| Confidence unavailable | `confidence = null` |
| Target not visible | `visibility_status = "not_visible"`, `unresolved_flag = true` |
| Ambiguous target | `visibility_status = "ambiguous"`, `unresolved_flag = true` |
| No warning | `warnings = []` |

Zero, page zero, empty crop IDs, fabricated evidence, and invented candidates
are never unknown sentinels.

## Deterministic normalization

The only accepted representation changes are:

- numeric string to `Decimal`;
- explicitly permitted integer string to integer;
- blank optional text to `null`;
- approved one-to-one camelCase alias to snake_case;
- approved enum case, space, or hyphen normalization.

The original direct transport object and normalized object remain separate in
memory and are never persisted with private values. Arbitrary wrappers,
unknown fields/enums/crops, wrong crop roles, visible values without evidence,
contradictory aliases, ambiguous numerics, and object/array shape changes fail
closed.

## Safe failure taxonomy

- `supplementary_transport_version_invalid`
- `supplementary_required_field_missing`
- `supplementary_field_type_invalid`
- `supplementary_enum_invalid`
- `supplementary_unexpected_field`
- `supplementary_evidence_reference_invalid`
- `supplementary_unplanned_crop_reference`
- `supplementary_internal_contract_invalid`

These failures are not collapsed into `supplementary_invalid_schema`,
`processor_failure`, or `ai_processing_failed`.

## Evidence-linkage hardening

Future V2 requests use `supplementary-crop-framing/2.0`. Each crop is an
immediately adjacent machine label/image pair:

```text
CROP_ID: <packet crop ID>
CROP_ROLE: <locally known role>
CROP_ORDINAL: <zero-based packet order>
TARGET_RELEVANCE: <target type and crop category>

[corresponding image part]
```

Every `crop_id` and crop role in the provider schema is a packet-specific enum
containing exactly the ordered crop identities authorized by that packet. A
packet-specific binding hash covers the provider schema, packet SHA-256,
ordered crop IDs and roles, transport version, and framing version.
Schema/packet drift or label/image ordering differences fail before dispatch.

Evidence is observation-local. `raw_visible_text`, the primary value, each
identity candidate, each financial component, each visible label, and every
contradiction candidate owns its own `evidence_refs`. No global provider
evidence list exists, and there is no implicit first-crop fallback. The model
supplies only bounded `crop_id` and `evidence_kind`. After validation, local
immutable metadata enriches each observation-local reference with crop role,
source page, plan ID, packet SHA-256, and source kind. This provenance
enrichment is local and is never delegated back to the provider.

Rules are deterministic: `visible` values require evidence; `ambiguous`
observations require evidence; `not_visible` values must be empty/null and may
have no references; each contradiction candidate must preserve separate
evidence. Unplanned crops and mismatched evidence kinds fail closed.

Safe telemetry now separates:

- `transport_validation_status`;
- `transport_normalization_status`;
- `evidence_validation_status`;
- `internal_observation_status`;
- `merge_status`;
- `reconciliation_status`.

The immutable historical smoke is logically: transport validation `passed`,
transport normalization `passed`, evidence validation `failed`, internal
observation `not_constructed`, merge `not_run`, and reconciliation `not_run`.

## Prepared future one-shot smoke

The offline preflight is prepared for the unchanged frozen packet and
`gemini-3.1-flash-lite`, with explicit crop framing, direct V2 schema, one
request, zero retry, and zero fallback. The completed smoke's authorization and
artifacts remain immutable; the new schema family is deliberately blocked from
execution until separately authorized.

Exact authorization required before that request:

> I authorize exactly one private supplementary V2 evidence-linkage smoke
> request using frozen packet SHA-256
> 385b8e3ef8f7bac593f07325d3df3a9e62a4629f5e4c7178ac39dd2e1e490b88,
> model gemini-3.1-flash-lite, explicit crop-label/image framing,
> packet-specific crop enums, and the direct supplementary-transport/2.0
> schema whose prepared schema-family fingerprint is
> 7143a627faf5ef0ffd969c3229fb12be0516710256b41e9f4a2d1ec6abd401d8.
> Gemini at generativelanguage.googleapis.com is the only authorized provider.
> The execution is limited to one request, zero retries, zero fallback, and the
> existing Phase A spend ledger and budget. Preserve the frozen packet/crop
> bytes, semantic question, target subtype, historical runs,
> AccountingDecisionEngine, AccountingReadiness, and source evidence. Do not
> run Arm B, paired A/B, Gate 5, Gate 10, simulated learning, Phase B, or Phase
> C. Do not persist the raw provider response, credentials, headers, private
> packet/crop bytes, private prompt/schema contents, or document-derived
> values. Stop after the one request and report safe fingerprints, transport,
> evidence and enrichment validation, usage, verified cost, latency,
> disposition, accepted/export flags, false-safe status, host and privacy
> assertions. Do not commit or push.
