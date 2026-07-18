# Accounting Assistant and Operator Rules

> Phase 4 note: this document describes the legacy vendor-neutral rule adapter. New vendor/property-aware customer policy work uses the tenant-isolated contracts documented in `PHASE_4_TENANT_GOVERNED_ACCOUNTING_POLICY_ENGINE.md`. Legacy rules are restricted to `local-default` and must not be restored as cross-tenant global policy.

## Conversational response boundary

The assistant has three deterministic routing modes, but provider-generated language in every mode:

- `lightweight`: greetings and basic orientation use a bounded low-cost text profile without invoice/chart payloads.
- `advisory`: accounting observations and questions use a reasoning profile with compact, provenance-labelled selected-invoice context and return natural prose rather than a forced JSON contract.
- `action`: explicit requests to change fields or create policy use the governed structured proposal contract.

Private chat context may include the sanitized original filename, filename stem, explicitly supplied parent-folder display names, parsed metadata candidates and parser warnings. It never includes an absolute filesystem path, drive letter or Windows username. Filename/folder metadata is labelled human-supplied and non-authoritative; the model must reconcile it with document facts and operator statements, keep conflicts visible, and cannot use it to overwrite raw source evidence silently.

Routing does not decide GL, readiness or export. Advisory turns cannot create mutations. If structured action extraction fails, the assistant returns a natural fail-safe response with no corrections or rules instead of discarding the conversation behind an HTTP 502. The failure code remains observable without storing provider bodies or credentials.

Status: implemented locally on 2026-07-15. Contract versions:
`accounting-assistant/1.0` and `operator-accounting-rule/1.0`.

## Purpose

The Invoice Assistant lets an operator discuss one processed invoice, inspect
schema-validated correction proposals, and decide whether a general policy
should become a reusable accounting constraint. It is advisory by design: a
model response cannot edit an invoice, activate a rule, decide the final GL,
change AccountingReadiness, or authorize export.

## Authority boundaries

```text
operator message + selected invoice facts + payable chart
  -> accounting reasoning profile
  -> validated correction proposals + optional DRAFT rule
  -> explicit operator action
       correction: existing save-edits API -> Pipeline V2 -> readiness refresh
       rule approval: ACTIVE semantic constraint
  -> active constraints filter/add compatible GLCandidates
  -> AccountingDecisionEngine selects selected_gl
  -> AccountingReadiness alone decides export_allowed
```

- AI proposals are never applied automatically.
- New rules are persisted as `draft` and are inert until explicit approval.
- Active rules only constrain or add semantically compatible `GLCandidate`
  objects. Their trace always has `selected_gl: null`.
- `AccountingDecisionEngine` remains the only final GL selector.
- `AccountingReadiness` remains the only readiness/export authority.
- A missing or invalid GL remains blocking; a rule cannot silence a blocker.

## Reusable rule contract

A rule scope can contain only semantic/source dimensions:

- `document_family`
- `line_family`
- `trade_family`
- `work_mode`
- `description_terms` with `any` or `all` matching

Vendor, person, invoice, account number, property, filename, and fixture
identities are rejected by the typed contract. Constraints may use explicit
payable GL codes and/or inclusive numeric minimum/maximum boundaries. Explicit
codes are checked against the current payable chart; a range that contains no
payable account is rejected.

Text terms match only preserved raw source fields in `_meta.source_text`.
Normalized text and generated descriptions are not substituted for source
evidence. Generated descriptions are provided to the chat as separately
labelled non-source context and cannot be cited as observed evidence.

## Lifecycle and auditability

Statuses are `draft`, `active`, `disabled`, and `rejected`.

Every lifecycle action appends a timestamped audit event with an actor. Edits
retain the rule ID and store a SHA-256 digest of the previous version. Rejected
and disabled rules are retained for audit but never applied. Rule updates are
written atomically and read through a file-version cache, so a large batch does
not parse the same rule file once per line.

Private runtime artifacts live under ignored `webapp_data` paths:

- `operator_accounting_rules/rules.json`
- `accounting_assistant/interactions/<interaction_id>.json`

No credential, request header, or provider response body is written by this
feature. The assistant uses the existing probe-verified accounting profile and
the existing provider client. The optional
`AI_MAX_ACCOUNTING_ASSISTANT_COST_USD` limit (default USD 0.02) fails closed
before a request whose estimated cost exceeds the budget.

## User interface

The sidebar contains two lazy-loaded modules:

- **AI Assistant**: batch/invoice context, conversation, correction cards,
  explicit apply action, and the explicit question “¿Quieres hacer de esto una
  regla determinística?” with approve/reject controls.
- **Accounting Rules**: rule list, semantic scope and GL constraint editor,
  approve/reject, enable/disable, and complete audit trail.

Applying a correction calls the existing save-edits endpoint. That path reruns
Pipeline V2 and refreshes AccountingReadiness; the chat never calculates either
result locally.

## Rollback and limitations

Removing the two sidebar routes disables operator access without changing
existing invoice processing. Disabling an approved rule removes its effect on
subsequent decisions while preserving history. Existing decisions are not
silently rewritten; invoices must pass through the normal edit/reprocess flow.

The feature is not self-training and does not infer rule scope from approval
history. It does not guarantee that a requested numeric GL range is a sound
semantic policy; the assistant must warn about that risk, catalog validation
must pass, and final semantic compatibility plus AccountingDecisionEngine still
control selection.
