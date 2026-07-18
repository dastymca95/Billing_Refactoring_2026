# Phase 4.4 — AI Deterministic Builder

## Goal

Allow an operator to improve an existing registered deterministic processor
from the vendor detail modal without editing Python. The workflow combines
private sample documents, natural-language conversation, typed declarative
changes, an isolated dry-run row preview and explicit human approval.

## Authority boundaries

The AI is advisory. It may propose values only for existing editable fields in
the vendor's declarative contract. It cannot:

- create or execute Python;
- activate its own proposal;
- select the final GL;
- decide AccountingReadiness;
- authorize export;
- publish learned behavior automatically.

`AccountingDecisionEngine` remains the only final GL selector and
`AccountingReadiness` remains the only export authority.

## Workflow

```text
registered vendor
→ private builder session
→ uploaded samples under webapp_data/deterministic_builder
→ normalized document evidence
→ accounting-profile conversation
→ schema-validated declarative draft
→ isolated processor dry-run with revision-bound config
→ selectable row/column preview
→ explicit operator approval
→ YAML backup + atomic write
→ future processing runs
```

Samples, extracted text and conversations are runtime-private and covered by
the existing `webapp_data/` Git ignore boundary. API responses contain the
safe original filename but never the private absolute source path.

## Preview routing

New detection patterns must be testable even when the current router would
classify a sample as unknown. `batch_processor.process_batch` therefore accepts
`forced_vendor_key` only when `dry_run=True` and only for a vendor already in
the canonical deterministic processor registry. Production processing cannot
use this override.

The builder creates a temporary batch, runs the real registered processor with
the draft YAML, captures at most 200 preview rows and removes the temporary
batch/config. No workbook, Dropbox upload or export is produced.

## Approval and rollback

Approval is rejected unless the current draft revision has a passing preview.
Any subsequent AI proposal or sample upload invalidates that preview. The
existing Vendor Rules service validates the patch, backs up the current YAML
and writes atomically. The backup remains available to the existing restore
workflow.

## Current boundary

The builder improves registered processors that already consume a verified
declarative configuration. Code-managed processors remain inspect-only until a
real declarative adapter is connected and tested. This prevents a browser from
pretending to edit logic that the runtime does not actually consume.
