# PHASE 4.1 — ResMan Context Data Hub
## Purpose

InnerView can operate file-first while the ResMan API is unavailable. A future
API adapter must publish the same canonical contracts and may not bypass the
snapshot, provenance, tenant, or audit boundaries described here.

```text
raw ResMan CSV
  -> report-aware parser
  -> preview + structural validation + diff
  -> operator publish
  -> immutable canonical snapshot
  -> manual overlays
  -> effective tenant context
```

## Active datasets

| Dataset | Raw report | Canonical use |
| --- | --- | --- |
| `vendors` | Vendor List | Exact ResMan vendor identities and operational metadata |
| `properties_units` | All Units | Property/unit directory; resident and lease PII is excluded |
| `gl_accounts` | Chart Of Accounts | GL existence, name, account type and payable status |
| `general_ledger` | General Ledger | Historical evidence and operator review; never a final GL selector |

Contract version: `resman-context-data/1.0`.

## Persistence and provenance

- Runtime data is tenant-scoped under `webapp_data/resman_context/`.
- The private raw CSV is copied without modification and identified by SHA-256.
- Snapshot rows record source snapshot, source row and deterministic row hash.
- The active snapshot may be rolled back transactionally.
- Manual additions, edits and removals are overlays. A removal is a tombstone,
  not destruction of the source snapshot.
- Update overlays preserve only fields changed by the operator. A later import
  can therefore update all other ResMan-owned fields.
- Raw report paths and contents are not returned by the API.

## Privacy minimization

The normalized vendor projection excludes ACH routing/account numbers and tax
identifiers. The units projection excludes residents, lease dates and deposits.
Those columns remain only inside the private raw runtime snapshot. `Reports for
APP/` and `webapp_data/` are Git-ignored.

## Accounting authority boundary

- Imported history is evidence, not an automatically activated learned rule.
- Vendor defaults and historical mappings do not write final GL values.
- Published chart rows extend the GL catalog identity/payability adapter, while
  approved semantic configuration retains semantic authority.
- Exact property and vendor adapters are backward-compatible additions.
- Only `AccountingDecisionEngine` may select `selected_gl`.
- Only `AccountingReadiness` may authorize export.

## UI and API

The sidebar exposes Vendors, Properties & Units, Chart of Accounts and General
Ledger. Each workspace supports search, pagination, CSV preview/publish,
snapshot history, rollback, audited add/edit and soft-delete.

The API prefix is `/api/resman-context`. The import protocol is deliberately
two-step: preview first, publish second. A malformed or modified staged source
cannot be published.

## Legacy adapters

- `Properties/Properties.csv` remains a temporary exact-name source for property
  abbreviations because the ResMan All Units report does not contain them.
- The committed/runtime chart remains the fallback when no tenant chart snapshot
  is published.
- The legacy canonical vendor CSV remains a fallback and is augmented by exact
  active vendor identities from the published tenant snapshot.

These adapters should be retired only after equivalent tenant datasets are
published and clean-checkout tests prove no processor depends on private assets.

## Future ResMan API

An API connector should implement a source adapter that produces the same
dataset kinds, canonical payloads and snapshot metadata. It must not write
directly to effective records or delete overlays. API sync should still show a
diff and use explicit publish/rollback semantics unless a tenant explicitly
enables a separately audited synchronization policy.
