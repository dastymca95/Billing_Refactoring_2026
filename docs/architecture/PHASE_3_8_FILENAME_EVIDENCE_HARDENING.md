# Phase 3.8 — Private filename evidence hardening

Reviewer 1 may inspect the original filename, filename stem, necessary relative
parent-folder display names, parser warnings, and non-authoritative metadata
candidates only in the loopback private labeling workspace rooted at
`INNER_VIEW_PRIVATE_BENCHMARK_ROOT`.

The UI labels this material **Source metadata evidence — verify against the
document.** It never exposes a drive letter, username, absolute filesystem path,
application prediction, GL decision, AI confidence, historical coding, Reviewer
2 label, or strong-reasoner output.

Raw filename and folder display values are preserved in an immutable private
sidecar on first access. Candidate interpretations use separate append-only
events with `confirmed`, `rejected`, `partially_correct`, `ambiguous`, or
`irrelevant` dispositions. Changing an interpretation cannot change the raw
metadata. A fingerprint mismatch fails closed and leaves the conflict visible.

Neither raw metadata nor candidate values are included in Git-safe status,
public traces, benchmark aggregate reports, normal telemetry, fixtures, or model
prompts. Private source-metadata sidecars stay below the private benchmark root.
Existing Reviewer 1 draft files, migration events, audit history, completion
status, and validation errors are not rewritten by this feature.

The frozen `selected_120_v1` snapshot and its SHA-256 remain authoritative and
unchanged. Source-metadata review events are labeling artifacts, not dataset
selection mutations.
