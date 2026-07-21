# Document Learning Experiment — Phase A Gate Report

Status: **HISTORICAL / SUPERSEDED**

> This report preserves the state of the original blocked Phase A attempt. It
> is not the current experiment status. Later work introduced the
> `CONTROLLED_EXTERNAL` Gemini-only contract and the supplementary transport
> V2 evidence model documented in the corresponding Phase A architecture
> reports. Historical counts and conclusions below are intentionally retained.

Date: 2026-07-18  
Branch: `experiment/document-learning-simulation`  
Scope: private-corpus inventory, eligibility, leakage-safe split, calibration design, spend controls, and local quality gates.

## Executive decision

The operator supplied explicit informed authorization limited to the frozen Phase A sample. The execution environment nevertheless denied the external private-data transfer before the Python runner was launched. No private document, derived fact, crop, provider response, request body, or telemetry artifact was sent or committed; provider calls, reservations, run directories, and experiment spend remain zero.

The current corpus can calibrate routing, coverage, latency, and cost once an institutionally permitted execution environment is available. It cannot yet provide statistically credible learning evidence because only 13 unique invoice units have defensible automated ground truth and none provides handwriting ground truth. Phase B remains prohibited.

## Phase 0 — Repository, privacy, and spend gate

- Correct experiment branch confirmed; no work was performed on `main`.
- The source corpus remains outside Git and the private experiment runtime is ignored.
- Tracked private source files: 0.
- Configured, enabled, credentialed, endpoint-configured, and priced profiles: 8 of 8.
- Credential values were never printed, serialized, or copied into experiment artifacts.
- The spend controller passed a synthetic zero-network dry run.
- Total phase caps are fail-closed across all providers: A USD 10; B USD 40 cumulative; C USD 200 cumulative. OpenAI and other-provider costs are also reported separately.
- Reservations occur before dispatch, dispatched attempts are tracked, and actual provider usage is recorded when available with a conservative fallback.
- Phase B requires an accepted Phase A; Phase C requires an accepted Phase B and explicit approval.
- Current worktree is intentionally not clean because experiment source changes are uncommitted under the requested Git policy.

## Phase 1 — Local inventory, zero API cost

All inventory work was local. No provider or network call occurred.

| Measure | Result |
|---|---:|
| Physical documents | 2,897 |
| Unique exact file hashes | 2,788 |
| Physical pages | 4,901 |
| Unique exact visual-page hashes | 4,592 |
| Estimated unique invoice identities | 4,195 |
| Total bytes | 1,947,886,928 |
| PDF documents | 2,243 |
| Image documents | 465 |
| Other/unsupported formats | 189 |
| Digital | 1,559 |
| Mixed | 12 |
| Scanned | 670 |
| Scan/photo images | 465 |
| Embedded-text documents | 1,571 |
| Single-page documents | 1,865 |
| Multi-page documents | 841 |
| Possible multi-invoice packets | 697 |
| Exact file duplicate groups | 92 |
| Exact visual-page duplicate groups | 209 |
| Combined exact leakage groups | 149 |
| Documents in exact leakage groups | 362 |
| Related invoice/version candidate groups | 230 |
| Documents in related candidates | 954 |
| Active deterministic-parser matches | 530 |
| Corrupt/unreadable | 2 |

Exact visual identity, not approximate similarity, is the only basis for reusable visual facts. Modified financial variants remain distinct facts and share a conservative leakage component so they cannot cross evaluation cohorts.

## Phase 2 — Eligibility and ground truth

| Eligibility class | Documents |
|---|---:|
| Accepted posted/evidence-reconciled ground truth | 16 |
| Deterministically reconcilable but requires independent GL label | 474 |
| Requires human adjudication | 2,216 |
| Unsuitable for learning evaluation | 191 |

The 16 accepted documents collapse to 13 unique invoice units after exact-duplicate grouping. Acceptance requires independently recomputed allocation reconciliation, nonblank vendor/property/invoice identity, payable GL, visible total evidence, and a single observable invoice identity. Prior AI output is never ground truth. Multi-invoice and partial packets are not automatically labeled.

The remaining critical limitation is the absence of evidence-backed human labels for handwriting, row concepts, PAID/crossed-out state, date provenance, and other row-level fields. Those cases remain in the adjudication population and are excluded from learning-accuracy denominators.

## Phase 3 — Leakage-safe split

Deterministic split seed and immutable, versioned private manifests are in place. Hidden answers are not embedded in the operational manifest.

| Cohort | Unique invoice units |
|---|---:|
| Training | 6 |
| Similar holdout | 3 |
| Unrelated holdout/control | 2 |
| Benchmark-only | 1 |
| Rule simulation | 1 |
| Total | 13 |

- Exact duplicate or linked-variant groups crossing cohorts: 0.
- Similar holdout without a training canonical family: 0.
- Unrelated canonical-family overlap: 0.
- Unrelated layout-family overlap: 0.
- One tenant-private vendor-family overlap remains explicitly reported; it is not hidden or represented as vendor-holdout evidence.
- Superseded split artifacts remain immutable and are marked rejected rather than overwritten.

## Phase 4 — Phase A calibration status

A 100-document calibration selection exists:

| Coverage dimension | Documents |
|---|---:|
| Total selected | 100 |
| Authoritatively labeled | 13 |
| Coverage-only/unlabeled | 87 |
| Digital | 51 |
| Mixed | 2 |
| Scanned | 24 |
| Scan/photo | 23 |
| Multi-page | 50 |
| Possible multi-invoice | 15 |
| Active deterministic parser | 40 |
| Handwriting ground truth | 0 |

The isolated runner is implemented to process ten-document shards, preserve stable result ordering, keep the experiment tenant separate, use deterministic processing first, disable native-PDF bulk use, and evaluate frozen outputs only after labels are unavailable to processing. It records extraction, reconciliation, candidate ranking, readiness, provider usage, latency, and cost without prompts, credentials, private filenames, or full paths in Git-safe output.

The authorized external run was attempted once, but the execution platform rejected the action before process launch. Therefore:

- provider calls: 0;
- OpenAI cost: USD 0.00;
- other-provider cost: USD 0.00;
- latency: not measured;
- baseline accuracy/generalization metrics: not measured;
- estimated-versus-actual cost comparison: not measured;
- false-safe export evaluation on the private Phase A sample: not measured.

Phase A remains blocked in this execution environment even though operator authorization was received. Its learning conclusion must remain insufficient unless an authorized execution environment completes the bounded run and the adjudicated holdout grows materially to include the missing critical document families.

## Phases 5–11 — Not started

Simulated UI corrections, benchmark submissions, approved-learning examples, governed-rule workflow, pre/post holdout evaluation, negative controls, the 500-document pilot, full-corpus execution, paid telemetry, and defect-driven benchmark reruns were not started. This preserves the required order: Phase A review before Phase B and explicit approval before Phase C.

No benchmark example changed production. No learning example selected a final GL. No rule was proposed, simulated, approved, or activated. `AccountingDecisionEngine` remains the final GL authority and `AccountingReadiness` remains the export authority.

## Phase 12 — Local validation status

| Gate | Result |
|---|---|
| Backend compile | PASS |
| Backend discovery | 515 tests |
| Full backend suite | 514 passed, 1 intentionally skipped, 6 subtests passed |
| Focused experiment suite | 40 passed |
| Knowledge Core, human-adjudication, and experiment focus | 61 passed |
| Frontend TypeScript/Vite build | PASS |
| Active Billing V2 Playwright | 14/14 passed |
| Repository safety scanner | PASS; 530 repository files scanned |
| Repository safety tests | 17 passed, 1 CI-only test skipped locally |
| Private/runtime trackable-path scan | 0 findings |
| Private corpus tracked by Git | 0 files |
| Experiment runtime ignored | PASS |
| `git diff --check` | PASS |

The local skip verifies import-only CI processor stubs and is expected unless the dedicated CI identity is enabled. No external-provider execution was part of these checks.

## Defects fixed during preparation

- Source hashes are revalidated between inventory and eligibility.
- Multi-invoice packets cannot receive a single automatic golden label.
- Modified financial variants cannot leak across cohorts.
- Canonical accounting family replaces GL-code hashing as the semantic split key.
- Historical reconciliation is independently recomputed.
- Runtime ignore status is verified through Git rather than inferred from ignore text.
- Experiment spend authority propagates through worker pools and fails closed when absent.
- Private-provider transfer has a separate explicit informed-authorization gate that fails before runtime creation or provider initialization.
- The experiment runtime override is rejected outside explicit experiment mode, so a stale value cannot redirect the normal application silently.
- Monetary totals remain typed as decimals even when there are zero calls.
- Legacy direct callers receive an internally computed conservative request-cost estimate rather than bypassing the spend gate.
- Spend telemetry rejects absolute/path-like labels and private document filename extensions; the experiment runner supplies only controlled identifiers to generic request tracing.

## Remaining risks

- Only 13 unique units have defensible automated ground truth.
- There is no handwriting ground truth.
- Row-level evidence-backed labels are not broad enough for the requested learning metrics.
- This execution environment does not permit transmission of the private corpus to external providers, even with the operator authorization recorded for this task.
- A statistically credible conclusion requires additional human adjudication and a larger hidden holdout.

## Recommendation

Do not begin Phase B. Phase A can proceed only in an environment institutionally permitted to transmit this private data, or against a rigorously de-identified corpus or approved local-only multimodal provider. Preserve the USD 10 all-provider cap and separately expand evidence-backed human adjudication before drawing a learning conclusion.

## Final classification

INSUFFICIENT EVIDENCE
