# Phase 4.2 — Invoice History and Ledger Reconciliation

## Purpose

`Invoice Detail` is the canonical AP history source. It complements rather
than replaces the General Ledger. Raw CSV files remain immutable tenant-local
evidence; published canonical records and manual overlays use the same
versioned ResMan Context Data Hub introduced in Phase 4.1.

## Authority boundaries

| Source | Authority |
|---|---|
| Vendor List | exact vendor identity |
| All Units | valid property and unit identity |
| Chart Of Accounts | GL existence, active state, and payability |
| Invoice Detail | invoice header and allocation history |
| General Ledger | posted accounting transactions and balances |

Historical data is candidate and reconciliation evidence only. It cannot
write `selected_gl`, authorize export, or override AccountingDecisionEngine or
AccountingReadiness.

## Canonical invoice allocation contract

Each record represents one source allocation and repeats only the required
parent invoice facts: vendor, invoice number, invoice/accounting/due dates,
invoice description and total, PO, allocation property, GL, description, and
amount. `invoice_occurrence_id` preserves repeated invoice numbers without
collapsing them. `allocation_index` preserves source ordering within that
occurrence.

The parser is a state machine for the ResMan hierarchy:

```text
vendor section
  -> invoice header
     -> one or more allocation rows
```

It does not infer missing sections or fabricate allocations. Invoice totals
are checked against the exact Decimal sum of their allocations.

## Deterministic reconciliation

Reconciliation first requires an exact normalized vendor name and exact
invoice reference. It then compares accounting date, property, GL, and signed
amount. Possible outcomes are:

- `matched_to_ledger`
- `posting_date_difference`
- `amount_mismatch`
- `gl_mismatch`
- `property_mismatch`
- `invoice_only`
- `matched_to_invoice_history` (General Ledger perspective)
- `ledger_only` (General Ledger perspective)

Every result carries evidence. `invoice_only` is not automatically an error:
the Invoice Detail and General Ledger snapshots can cover different periods.
No fuzzy matching or AI is used for this join.

## Published local baseline

- Invoice Detail snapshot: `rms_4890a593417842a3`
- SHA-256: `80c58f5ca862126d2a0f561f51d0f62b8014e51fe509b320cad7c2244450bad2`
- Canonical allocations: 37,296
- Parser errors/warnings: 0 / 0
- Invoice headers represented: 18,841
- Header/allocation total mismatches: 0
- Vendor sections matching Vendor Master: 552 of 552 unique normalized names
- Allocations with valid property: 37,296
- Allocations with valid GL: 37,296

Reconciliation against the currently published General Ledger:

- matched to ledger: 18,428
- amount mismatch: 17
- posting date difference: 3
- invoice only / outside current ledger evidence: 18,848
- GL mismatch: 0
- property mismatch: 0

These are regression and data-quality metrics, not model benchmark results.

## Legacy and rollback

The legacy General Ledger parser remains active and unchanged as a source of
posting evidence. Query-time vendor resolution remains exact-only. Any prior
snapshot can be reactivated; raw reports and audit events remain preserved,
and tenant overlays are reapplied. Deactivating or rolling back Invoice Detail
does not alter invoice processing decisions already persisted elsewhere.
