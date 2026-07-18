# PHASE 4 — Tenant-Governed Accounting Policy Engine
## Status and scope

Phase 4 introduces the governed runtime foundation needed for customer-specific accounting automation. It does **not** implement autonomous self-training, automatic policy activation, subscription management, or the full ResMan onboarding workflow.

The supported flow is:

```text
human/AI conversation
→ typed tenant policy draft (inert)
→ historical batch simulation
→ conflict and amount-mismatch review
→ explicit tenant-admin approval
→ active candidate constraint
→ AccountingDecisionEngine selects selected_gl
→ AccountingReadiness authorizes export
```

## Active contracts

- `tenant-vendor-entity/1.0`: tenant-owned canonical vendor identity, ERP identifier, aliases and audit trail.
- `tenant-accounting-policy/1.0`: versioned declarative scope and allowed payable GL action.
- `tenant-policy-simulation/1.0`: immutable summary of the policy version and line snapshot evaluated before approval.
- `accounting-assistant/1.0`: advisory chat response extended with an optional inert tenant policy draft.

Runtime records live under:

```text
WEBAPP_DATA_ROOT/tenant_accounting/<tenant_id>/vendor_entities.json
WEBAPP_DATA_ROOT/tenant_accounting/<tenant_id>/policies.json
```

`WEBAPP_DATA_ROOT` is already Git-ignored. These files are customer runtime data, not source code or public fixtures.

## Tenant boundary

`INNER_VIEW_TENANT_ID` is the temporary deployment adapter until authenticated account claims provide tenant context. Local development defaults to `local-default`. A production deployment fails closed when the variable is absent and rejects a request that attempts to override the configured tenant.

Every vendor entity and policy carries `tenant_id`; storage paths, reads, writes, resolution, simulation and candidate application validate it. The legacy global operator-rule store is only applied to `local-default`. It is explicitly skipped for every non-default tenant to prevent historical rules from leaking between customers.

## Vendor identity

Observed invoice text is not treated as the ERP identity. `TenantVendorEntity` preserves:

- canonical display name;
- optional ERP/ResMan vendor ID;
- human-approved aliases;
- creation/update provenance and audit events.

Alias resolution is exact after generic Unicode/case/whitespace normalization. Ambiguous aliases fail unresolved. No vendor name, property, invoice or fixture is hardcoded in Python.

## Policy contract

A policy may scope by a reusable combination of:

- tenant vendor entity;
- tenant property IDs;
- document family;
- line family;
- trade family;
- work mode;
- raw source-description terms.

The action contains payable `allowed_gl_codes` and optional expected-amount/tolerance behavior. Source matching reads provenance-preserved raw source fields; it never matches generated descriptions.

Vendor identity alone cannot bridge missing semantic catalog metadata. A compound line-level semantic or source condition is required. Explicitly incompatible work modes fail closed.

## Lifecycle and authorization

1. `draft`: inert and editable.
2. `simulated`: current policy version was evaluated against a reproducible line snapshot.
3. `active`: explicitly approved after a simulation with at least one match and zero blocking conflicts.
4. `disabled`: previously approved but not applied.
5. `rejected` or `superseded`: inert terminal/history states.

Editing any draft, simulated, active or disabled policy increments its version, invalidates the prior simulation and returns it to `draft`. Approval is rejected when the simulation is missing, belongs to another policy version, has no matched historical line, or reports a blocking conflict.

The UI intentionally provides no direct “approve” action for a chat proposal. The user must open **Accounting Rules → Tenant policies**, select a historical batch, simulate, inspect the results and then approve.

## Candidate-only enforcement

The engine can remove incompatible candidates or add an approved payable candidate with policy provenance. It never writes `GL Account` directly and its trace always reports `selected_gl: null`.

If simultaneously active policies have disjoint GL sets, or no approved GL can produce a semantically compatible payable candidate, the result is an explicit `tenant_policy_conflict` blocker. Expected-amount mismatches follow the configured `review` or `warning` behavior.

Only `AccountingDecisionEngine` selects the final GL. Existing accounting decision versioning, explanations and alternatives remain unchanged. Only `AccountingReadiness` determines Ready/export authorization.

## API and UI routes

- `GET /api/tenant-accounting/context`
- `GET|POST /api/tenant-accounting/vendors`
- `GET|POST /api/tenant-accounting/policies`
- `PUT /api/tenant-accounting/policies/{policy_id}`
- `POST /api/tenant-accounting/policies/{policy_id}/simulate`
- `POST /api/tenant-accounting/policies/{policy_id}/decision`
- `POST /api/tenant-accounting/policies/{policy_id}/status`
- `POST /api/accounting-assistant/chat` accepts optional `tenant_id` through the same validated context adapter.

The Accounting Rules workspace exposes tenant policies, batch simulation reports, explicit approval/rejection, enable/disable controls, vendor entities and policy audit information. The floating chat displays proposed tenant drafts as inert and directs the user to the governance screen.

## Legacy adapters

`operator_accounting_rules.py` remains temporarily for existing vendor-neutral local rules. It has no tenant vendor identity and is not a migration target for new customer-specific policies. It is skipped for non-default tenants. A later controlled migration may translate eligible rules into explicit tenant policies after tenant ownership and simulation; there is no automatic migration or activation.

## Rollback behavior

- Disable an approved policy through its status endpoint/UI; the audit event remains.
- Editing invalidates activation and simulation rather than mutating an active rule silently.
- Removing the Phase 4 integration call returns processing to existing legacy/V2 candidates, but this is a code rollback and not a runtime feature flag.
- AccountingDecisionEngine and AccountingReadiness remain the safety boundaries throughout rollback.

## Explicit non-goals

- no automatic learning from customer corrections;
- no cross-tenant sharing of private documents, labels, vendor identities or policies;
- no automatic “global best practice” promotion;
- no ResMan credential ingestion or full account scan yet;
- no subscription/billing implementation;
- no GPT/model/routing changes;
- no self-training or silent policy activation.

## Path to the product vision

The next product layers can build on these contracts: authenticated tenant claims, read-only ResMan onboarding, vendor frequency/variance analysis, deterministic/hybrid/AI-first recommendations, progress dashboards, and privacy-preserving aggregate templates. Shared templates must be learned only from anonymized/adjudicated patterns and must still arrive as tenant-visible drafts requiring simulation and approval.
