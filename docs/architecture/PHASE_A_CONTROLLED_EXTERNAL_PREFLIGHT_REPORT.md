# Phase A Controlled External Preflight Report

Date: 2026-07-19  
Branch: `experiment/document-learning-simulation`  
Status: **HISTORICAL / SUPERSEDED — SYNTHETIC PREFLIGHT PASSED; PRIVATE DISPATCH WAS NOT AUTHORIZED**

> Historical scope: this report preserves the original Gemini + DeepSeek
> preflight facts. It is not the current authorization contract. The active
> controlled Phase A contract is Gemini-only for authorized visual extraction
> and verification. Semantic normalization and candidate generation remain
> local; DeepSeek, OpenAI, Claude, and fallback providers are not selected,
> prepared, reserved, or contacted in `CONTROLLED_EXTERNAL` mode.

## Preserved local feasibility decisions

The prior reports and their conclusions remain unchanged:

- `PHASE_A_LOCAL_MULTIMODAL_REPORT.md`: **LOCAL RUNTIME NOT FEASIBLE**
- `PHASE_A_LOCAL_INSTRUCT_MULTIMODAL_REPORT.md`: **LOCAL INSTRUCT RUNTIME NOT FEASIBLE**

No prior local telemetry was reinterpreted, overwritten, or deleted.

## Execution modes

`CONTROLLED_EXTERNAL` is an experiment-only execution mode. It coexists with
`LOCAL_ONLY` and normal application routing. A missing mode does not enable the
external experiment. `CONTROLLED_EXTERNAL` additionally requires an active
context-local controller, a per-document scope, the persistent Phase A spend
gate, a frozen manifest, and—before any private request—a versioned private
authorization record.

The old command-line boolean that acknowledged private transfer is no longer
sufficient by itself. It is retained only as a backward-compatible first gate;
private dispatch also requires the authorization record and manifest hash.

## Historical network allowlist used by this preflight

| Purpose | Provider | Exact HTTPS host |
|---|---|---|
| Facts-only visual extraction | Gemini | `generativelanguage.googleapis.com` |
| Candidate-only semantic support | DeepSeek | `api.deepseek.com` |

HTTP, alternate ports, user-info URLs, look-alike subdomains, OpenAI, Claude,
Anthropic, and all other destinations are blocked before transport. Redirects
are blocked in this execution mode. There is no provider fallback.

## Frozen-manifest authority

The active Phase A calibration manifest contains 100 selected documents. Its
SHA-256 is verified when the controller is created. Document content hashes are
resolved from the immutable private inventory. A private document must match an
exact selected content hash; similarity is not sufficient. The controller
rejects manifests with more than 100 assignments, embedded answers, unresolved
hashes, or duplicate content hashes.

Manifest, inventory, authorization, payload, response, and telemetry files must
remain below the ignored private experiment root.

## Gemini payload contract

Gemini receives:

- inline source page/image pixels needed for that request;
- a facts-only extraction instruction;
- the typed visual facts schema.

Local OCR helper text is not transmitted in `CONTROLLED_EXTERNAL`; it is merged
locally after extraction. The request contains no GL catalog, expected GL,
tenant learning result, readiness decision, unrelated accounting policy,
holdout label, or correction answer. Unknown values remain null/unknown.

The integration uses inline chat-completion media. It does not use Gemini Files,
File Search, explicit context caching, grounding, Interactions, or Live APIs.

## Historical DeepSeek payload contract

DeepSeek receives a separate derived-facts request with:

- an experiment-scoped opaque document ID;
- opaque sequential line IDs;
- redacted normalized line text;
- quantity, unit price, and amount when present;
- current non-authoritative semantic taxonomy hints.

An allowlist builder removes document IDs, source line IDs, paths, filenames,
account numbers, addresses, person names, ground truth, holdout labels, and
correction results. The transport rejects binary, image, PDF, data-URL, local
path, or document filename content. It sends no source document and no GL
catalog. The response can support semantic candidates only.

`AccountingDecisionEngine` remains the only component that selects final GL.
`AccountingReadiness` remains the only component that authorizes export.

## Spend controls

- Persistent experiment ledger: one ledger across Gemini and DeepSeek.
- Phase A combined cap: USD 10.000000.
- Reservation: required before every transport attempt.
- Retries/reruns: each attempt creates a persistent reservation.
- Settlement: requires provider-reported usage and configured input/output
  pricing in `CONTROLLED_EXTERNAL`.
- Indeterminate usage or pricing: conservatively charges the reservation,
  cancels outstanding work, and blocks later dispatch.
- Alerts: 50%, 75%, 90%, and 100% of projected Phase A spend.
- Reporting: provider/profile/model split plus combined spend.
- Ledger fields: provider, model, profile, document SHA-256, purpose, estimate,
  reservation, normalized usage, actual/charged cost, and failure code.

Synthetic preflight spend:

| Provider/profile | Requests | Estimated USD | Actual/charged USD |
|---|---:|---:|---:|
| Gemini / `gemini-vision` | 1 | 0.000224 | 0.000339 |
| DeepSeek / `deepseek-accounting` | 1 | 0.000067 | 0.000089 |
| **Combined** | **2** | **0.000291** | **0.000428** |

Open reservations after preflight: **0**.

## Synthetic-only preflight results

No private document was opened or transmitted. No provider response was
persisted.

1. Gemini synthetic visual call: **PASS**.
2. DeepSeek synthetic derived-facts call: **PASS**.
3. Unauthorized host blocked before transport: **PASS**.
4. Document outside frozen manifest blocked: **PASS**.
5. DeepSeek binary/source media rejected: **PASS**.
6. Holdout labels removed: **PASS**.
7. Local paths and filenames removed: **PASS**.
8. Spend reservation survives controller reopen/rerun: **PASS**.
9. Exhausted projected budget blocks before dispatch: **PASS**.
10. OpenAI/Claude/other remote fallback blocked: **PASS**.
11. AccountingDecisionEngine final-GL authority exercised: **PASS**.
12. AccountingReadiness export authority exercised with blank-GL blocker:
    **PASS**.
13. Private experiment root and telemetry are Git-ignored: **PASS**.

## Provider policy review

### Gemini

Official references:

- <https://ai.google.dev/gemini-api/docs/zdr>
- <https://ai.google.dev/gemini-api/docs/billing>
- <https://ai.google.dev/gemini-api/docs/files>

Google documents that Paid Services do not use prompts or responses to improve
products. It also documents limited abuse-monitoring logs unless project-level
zero-data-retention approval applies. Some features have separate persistence:
Files storage, grounding, stateful Interactions/Live APIs, and explicit cached
content. The experiment avoids all of those features and uses one stateless,
inline request per page/image. Consequently, there is no uploaded File API
object to delete after processing.

This code cannot verify from the API key alone that the specific project is
shown as paid, that a ZDR request was approved, or which account-level sharing
settings the owner selected. Those remain operator confirmations.

### DeepSeek

Official references:

- <https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html>
- <https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html>
- <https://api-docs.deepseek.com/quick_start/rate_limit>

The public materials describe collection/processing of inputs and outputs and
do not provide an account-verifiable API flag for zero retention. The published
privacy materials also do not establish a short, API-specific retention period
or a self-service zero-retention control that this application can verify. The
Open Platform terms put downstream end-user privacy disclosure obligations on
the developer. DeepSeek's API documentation warns not to put privacy data in
`user_id`; this experiment sends no person identity in that field.

Therefore DeepSeek receives only minimized derived facts, but retention,
training-use, storage location, contractual DPA status, and account-specific
controls remain unresolved operational risks. They require explicit owner
review before private dispatch.

## Historical operator action proposed by this preflight

Private dispatch is currently fail-closed because no accepted private
authorization record was created by this task. The operator must review the
actual Gemini and DeepSeek account/project settings and place a versioned
authorization record under the ignored private experiment root. The record must
bind the experiment ID, exact frozen-manifest SHA-256, operator identity,
timestamp, policy confirmations, and the SHA-256 of this exact text:

> I authorize the Innerview document-learning experiment to transmit only the documents in the frozen Phase A manifest (maximum 100 documents) to the paid Gemini API for facts-only visual extraction, and to transmit only minimized derived facts to the DeepSeek API for candidate-only semantic reasoning. I understand that source documents may contain PII, addresses, financial amounts, account numbers, and confidential business information. I confirm that I reviewed and accepted the applicable Gemini and DeepSeek account-level data-use and retention settings. I do not authorize Phase B, Phase C, the full corpus, OpenAI native-PDF, Claude, arbitrary provider fallback, source documents to DeepSeek, ground truth or holdout labels to any provider, or private runtime artifacts in Git. The combined Phase A provider budget is USD 10. AccountingDecisionEngine remains the only final GL authority and AccountingReadiness remains the only export authority.

Until that exact authorization and the account-policy confirmations are
present, the required state is:

**blocked_waiting_for_private_provider_policy_confirmation**

No Phase A execution, private provider call, commit, or push was performed by
this preparation task. This historical authorization text is superseded and
must not be used to authorize a current run.
