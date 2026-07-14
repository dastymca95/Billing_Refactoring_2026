# Phase 3.9A — Assisted labeling workspace hardening

## Scope and authority

The full-manual Reviewer 1 pilot is suspended. The dataset owner's private
decision closes validity triage for all members of `selected_120_v1`; it does not
assert facts, responsibility, property, reimbursement, GL, label completion, or
gold status. Reviewer 2 remains disabled and strong accounting reasoning remains
shadow-only.

## Reproduced workspace problems

- The served header and workflow still described Phase 3.7 blind labeling.
- Obsolete keep/exclude triage remained visible after final human adjudicability.
- A large JSON textarea was the primary editor.
- Fixed viewport heights made the queue, preview, and form compete for space.
- The right-side actions could fall below the fold.
- Timer state was coupled to document loading and did not communicate pause state.
- Autosave was driven by textarea input and could duplicate requests or disrupt focus.
- Navigation had no dirty-field guard.
- Validation errors were rendered as an undifferentiated JSON payload.
- Preview rotation controls did not persist a reviewer adjustment.
- Line operations and exception-first review were unavailable.

## Hardened workflow

The loopback-only workspace now uses independently scrollable queue, preview, and
structured-label panels. Raw proposal JSON is an optional diagnostics disclosure,
not an editing requirement. Responsive CSS removes fixed textarea sizing and
keeps approval actions sticky within the right panel.

Machine proposals are stored privately as `machine_proposed` and `unverified`.
Each field carries field-specific confidence, provenance, evidence, conflicts,
and profile version. Accept, correct, reject, unknown, unreadable, and not-
applicable decisions are separate append-only human events. Corrections retain
the original proposal. Existing Reviewer 1 drafts are never overwritten.

Document approval fails closed against the committed Reviewer 1 validator and
requires explicit document inspection plus a decision for every proposal. The
resulting status is `human_verified_assisted`, never `adjudicated_gold`.

The workspace provides previous/next navigation, exception-only filtering,
field acceptance, safe acceptance of inspected non-conflicting proposals,
validation navigation, guarded keyboard shortcuts, pause/resume, zoom, persistent
non-destructive rotation, and explicit duplicate/split/merge/copy/apply operations
over selected human-draft lines. Split and merge fail visibly to unknown when an
amount cannot be safely derived, requiring reviewer resolution. There is no
bulk approval method and the queue remains restricted to `pilot_20_v1`.

## Privacy

Proposals, human decisions, dataset adjudication events, metrics, filenames,
folders, and document details remain below `INNER_VIEW_PRIVATE_BENCHMARK_ROOT`.
No private labels or source metadata are included in Git-safe output or normal
application telemetry.

## Prepared pilot baseline

- Assisted proposals generated: 20 / 20.
- Observable fields proposed: 314.
- Line items proposed from observable text: 68.
- Line semantic classifications proposed: 68.
- AccountingDecisionEngine GL candidates proposed: 13.
- Proposal status: 100% unverified and non-authoritative.
- Human-accepted fields: 0.
- Human-corrected fields: 0.
- Reviewer 1 completed documents: 0 / 20.
- Adjudicated-gold documents: 0.
- Strong-reasoner executions: 0.

These are preparation counts, not model-quality results. Proposal acceptance,
correction rates, review time, filename usefulness, and validation workload can
only be interpreted after the human-assisted pilot runs.
