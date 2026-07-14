# Phase 3.9C — Multimodal Provider Activation and Capability Validation

## Scope and safety state

This phase adds probe-backed provider discovery. It does not change
`AccountingDecisionEngine`, `AccountingReadiness`, adjudication thresholds,
the `selected_120_v1` dataset, or labeling state. Strong accounting reasoning
remains shadow-only. No model is selected by a hardcoded name.

## Runtime configuration audit (2026-07-14)

The reproducible checkout had no configured runtime provider profiles:

- configured providers: 0;
- configured models: 0;
- provider credentials present: 0 (presence only; values were never read into a report);
- declared capabilities: 0;
- runtime-verified capabilities: 0;
- enabled profiles: 0;
- disabled profiles: 0 (there was no profile to evaluate).

Consequently, no real multimodal or reasoning provider could be activated in
this environment. This is an external deployment/configuration blocker, not an
adjudication-policy failure. The validator exits with status 2 and the
autonomous gateway remains disabled. It does not manufacture capability
advertisements and it does not rerun the autonomous pilot without a verified
provider.

## Existing adapter and client behavior

The committed provider client supports `openai` and `openai_compatible`
chat-completions transports, strict JSON responses, text content, image input,
and page-image fallback. Runtime configuration is loaded from `AI_*`
environment variables. The client uses a 45-second default timeout. Existing
text and vision call paths retain their bounded retry policies; the capability
validator adds no hidden retry loop.

The configured model ID, endpoint, credential presence, and private probe
evidence are separate inputs. Credential values, full keys, private image
references, and probe markers are never serialized into capability reports.

## Capability contract

`ModelProfileCapabilityReport` records declared, verified, and unavailable
capabilities independently for every profile. Supported capability names are:

- `text_extraction`;
- `visual_document_understanding`;
- `handwriting_interpretation`;
- `structured_output`;
- `long_document_processing`;
- `accounting_reasoning`;
- `independent_verification`.

A declaration is policy metadata only. Each capability must pass its own live
probe before it enters `verified_capabilities`. Missing credentials or endpoint
configuration disables the profile without attempting a network request.

## Activation state machine

The autonomous gateway is enabled only when the verified reports collectively
contain:

1. visual document understanding plus structured output;
2. accounting reasoning plus structured output;
3. independent verification.

Partial success is reported as degraded and cannot satisfy the complete gate.
The activated Phase 3 model registry contains only roles proven by probes.
Strong accounting reasoning is fixed to `shadow` mode by the activation
contract; this phase contains no production promotion path.

## Reproduction

Run from the repository root with deployment-owned environment configuration:

```powershell
python scripts/validate_provider_capabilities.py `
  --private-output .private/phase_3_9c/provider_capabilities.json
```

Exit 0 means the complete activation gate passed. Exit 2 means the report is
valid but one or more required verified capabilities are absent. The console
prints only aggregate, Git-safe state. The detailed report is written beneath
the ignored `.private` directory.

## Activation prerequisites

Deployment must provide authorized model IDs, endpoint configuration where
required, credentials, and a private visual probe with a known marker. After a
successful live run, archive the private report under the authorized benchmark
root and rerun the Phase 3.9B pilot in shadow mode. Do not lower adjudication
thresholds to obtain coverage.

## Phase 3.9C-1 runtime topology

Runtime configuration now materializes four logical profiles with independent
profile IDs and namespaces:

| Profile | Role | Required model variable |
|---|---|---|
| `runtime-text` | text extraction | `AI_MODEL` |
| `runtime-vision` | multimodal and handwriting extraction | `AI_VISION_MODEL` |
| `runtime-verification` | isolated verification | `AI_VERIFICATION_MODEL` |
| `runtime-accounting` | accounting reasoning | `AI_ACCOUNTING_REASONING_MODEL` |

Each specialized profile accepts its own provider, API key, and base URL, then
falls back to the corresponding base `AI_*` value when omitted. Secrets are
excluded from Pydantic serialization and capability reports. Trace and cache
namespaces include the logical profile ID; probe prompts also contain
role-specific boundaries.

Verification never claims independent-family voting. An explicit matching
model family, or the same provider and exact model ID, is recorded as
`isolated_same_family`. All other configurations remain
`isolated_unconfirmed_family` until deployment supplies reliable family
metadata.

## Acceptance status

- Typed capability contract: complete.
- Probe-backed declared-versus-verified separation: complete.
- Secret-safe audit and private detailed output: complete.
- Conservative activation state machine: complete.
- Strong reasoner shadow-only: complete.
- One real multimodal provider verified: blocked by missing runtime profile and credentials.
- One real reasoning provider verified: blocked by missing runtime profile and credentials.
- Dataset or label changes: none.
