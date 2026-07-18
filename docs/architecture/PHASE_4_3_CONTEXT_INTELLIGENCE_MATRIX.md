# Phase 4.3 — Context Intelligence Matrix

## Deterministic processor coverage

The vendor matrix now joins historical ResMan evidence to the committed
processor registry through `deterministic-coverage/1.0`. A YAML file by itself
does not qualify a vendor as deterministic: the vendor must have a registered,
loadable processor entrypoint. Vendor identity matching is exact after basic
normalization and uses the configured display name and aliases; fuzzy matches
are intentionally rejected.

The Parser column distinguishes active processors from vendors without a
registered processor. Double-click detail exposes the processor module,
entrypoint and configuration provenance. Existing declarative string-list
patterns can be edited through the validated Vendor Rules API. Each save
creates a backup and preserves non-edited YAML. Python implementation logic is
inspect-only; code-managed processors cannot be edited from the browser.

These edits affect future deterministic parsing only. They never activate
learned rules, select a final GL, change AccountingDecisionEngine, or bypass
AccountingReadiness/export authorization.

## Purpose

Context Intelligence is the onboarding bridge between the current CSV-backed
ResMan simulation and a future direct ResMan API connection. An operator must
explicitly press **Scan ResMan**. Until that action, no matrix exists and the
workspace shows `not_generated`.

## Source boundary

Each scan reads the five active tenant snapshots:

1. Vendor List — vendor identity.
2. Properties & Units — property and unit identity.
3. Chart of Accounts — GL existence and payability.
4. Invoice Detail — invoice and allocation history.
5. General Ledger — independent posting evidence.

The generated snapshot records all five SHA-256 hashes. Any later source
publication makes the matrix `stale` and removes it from accounting candidate
consumption until the operator scans again.

## Observable facts and recommendations

Statistics are deterministic facts:

- invoice and allocation counts;
- total and average historical amount;
- active months and history span;
- GL frequency, amount, and share;
- property frequency, amount, and share;
- property-specific GL frequency;
- property-level GL and vendor frequency.
- exact vendor/property posting counts and signed totals from General Ledger;
- human-readable GL names from the published Chart of Accounts.

The deterministic-candidate score is an onboarding recommendation based on
volume, active months, monthly coverage, and dominant-GL concentration. It is
not AI confidence and is not accounting readiness. Thresholds are universal;
there are no vendor, property, invoice, or fixture exceptions.

Possible recommendations:

- `deterministic_candidate`
- `review_candidate`
- `variable`
- `insufficient_history`

## Human governance

Double-clicking a vendor opens the complete profile without expanding the
matrix row. The operator may store one of:

- `unreviewed`
- `approved_candidate`
- `needs_review`
- `excluded`

Reviewer notes, actor, timestamp, and audit event are persisted separately
from the source datasets. Re-scanning preserves these overrides. Approval does
not create or activate an accounting rule.

## Accounting integration

A current, non-stale matrix may produce up to three historical
`GLCandidate` objects for an exact vendor and optional property match. They
carry source snapshot, frequency, share, amount, governance state, and
`selection_authority=false`.

The integration remains:

```text
Context Intelligence historical frequency
  -> GLCandidate
  -> semantic compatibility and tenant policy checks
  -> AccountingDecisionEngine
  -> AccountingReadiness
```

Context Intelligence never writes `selected_gl`, never authorizes export, and
never changes required-field or readiness behavior. Candidate lookups are
cached per tenant/vendor/property and invalidated on every source publication,
scan, or governance update.

## User-controlled validation state

The real `local-default` workspace is intentionally left in
`not_generated`. Automated tests use isolated temporary tenant data. The first
real matrix will only be created when the operator presses **Scan ResMan** in
the new sidebar workspace.
