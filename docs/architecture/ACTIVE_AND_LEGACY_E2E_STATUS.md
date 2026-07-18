# Active and legacy E2E status

## Active product shell

Billing V2 is the active InnerView invoice-processing shell. Current release
validation must exercise the Billing V2 toolbar, processed invoice table,
document viewer, backend-authored `AccountingReadiness`, export authorization,
processing-route controls, floating accounting assistant, governed tenant
rules, and inline human adjudication.

The active browser gates are:

- `e2e/billing-v2.spec.ts`
- `e2e/readiness-gate.spec.ts`
- `e2e/human-adjudication.spec.ts`
- the Billing V2 paths in `e2e/accounting-assistant.spec.ts`
- `e2e/context-intelligence.spec.ts`
- `e2e/resman-context-data.spec.ts`

These gates may use isolated API fixtures, but they must not weaken production
readiness, export, tenant, or provider authorization behavior.

## Historical legacy-shell suites

Several historical specs still assume that the retired legacy workspace is
the default route. They look for selectors and layout behaviors such as
`template-batch-selector`, the permanent batch explorer, old panel windows,
and direct legacy navigation before testing their original feature.

The principal migration candidates are:

- `e2e/operator-visual.spec.ts`
- `e2e/utility-u4.spec.ts`
- `e2e/ingestion-ai9.spec.ts`
- `e2e/reviewer-assisted-workspace.spec.ts`

Failures caused solely by those retired-shell selectors are not Billing V2
product regressions. They are also not grounds for deleting tests or lowering
assertions. Historical tests retain value as migration references.

## Controlled cleanup plan

1. Keep active Billing V2 gates in the current release configuration.
2. Classify each historical spec as migrate, archive as a legacy suite, or
   retain as a non-release compatibility test.
3. Migrate accounting, export, evidence, and safety assertions before visual
   shell assertions.
4. Preserve legacy tests in a clearly named suite until their replacement has
   equivalent coverage.
5. Never make the test count green by deleting a failing historical test.

The preservation snapshot records both the green active gates and the known
legacy-suite debt. Cleanup of legacy E2E remains a separate, reviewable change.

## Explicit runners

- `npm run test:e2e` and `npm run test:e2e:active` execute the active Billing
  V2 release gate.
- `npm run test:e2e:legacy` executes the preserved historical specs through
  `playwright.legacy.config.ts`. It is a migration/audit suite, not a release
  gate, and its failures must remain visible and documented.
