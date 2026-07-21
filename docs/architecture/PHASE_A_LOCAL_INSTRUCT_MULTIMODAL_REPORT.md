# Phase A Local Instruct Multimodal Evaluation

Date: 2026-07-19  
Branch: `experiment/document-learning-simulation`  
Commit baseline: `a5b7bbad479acde2fd24b5066db21fd351b1cabd`  
Classification: **LOCAL INSTRUCT RUNTIME NOT FEASIBLE**

## Independent evaluation scope

This report evaluates only `qwen3-vl:2b-instruct`. It does not reinterpret or
overwrite the preserved `qwen3-vl:2b` thinking result in
`PHASE_A_LOCAL_MULTIMODAL_REPORT.md`.

The frozen 100-document sample, accepted leakage-safe split, source files, and
prior private telemetry were not changed. No real sample document was opened
for this evaluation because the mandatory synthetic visual gate failed first.

## Installation and hardware profile

- Ollama 0.32.1
- exact tag: `qwen3-vl:2b-instruct`
- manifest/model ID: `ea422f1e7365`
- model family: `qwen3vl`
- parameters: 2.1B
- quantization: Q4_K_M
- disk size reported by Ollama: 1.9 GB
- capabilities reported by Ollama: completion, vision, tools
- hardware: Ryzen 9 5900HX, 23.37 GiB RAM, RTX 3050 Ti Laptop, 4 GiB VRAM

The manifest exists under the user's local Ollama model store outside the
repository. Model weights tracked by Git: zero. Ollama remained bound to the
literal loopback address `127.0.0.1`; cloud use and remote fallback were
disabled.

## Separate profile

The instruct evaluation does not reuse thinking-model telemetry identifiers.
It defines:

- visual extraction: `local-qwen3-vl-2b-instruct`
- text extraction: `local-qwen3-vl-2b-instruct-text`
- verification: `local-qwen3-vl-2b-instruct-verification`
- accounting candidate support: `local-qwen3-vl-2b-instruct-accounting`

Configuration remains facts-only/candidate-only, `think=false`, deterministic
temperature/seed where supported, 8,192-token context, bounded output, and one
local invoice worker. Extraction receives no GL catalog, readiness/export
instructions, historical answer, holdout label, or unrelated tenant policy.

`AccountingDecisionEngine` and `AccountingReadiness` were not modified.

## Mandatory synthetic probes

The probes use only generated public-safe text and a generated high-contrast
image. The runtime was protected by the same adapter and process socket gates
used for the private experiment.

| Probe | Result | Latency | Response observation |
|---|---:|---:|---|
| Text JSON | PASS | 0.912 s | Valid JSON in `message.content`; no thinking recovery |
| High-contrast visual extraction | FAIL | 55.945 s | Non-JSON content; prior attempt produced malformed/truncated JSON |
| Exact candidate-only semantic envelope | PASS | 3.698 s | Typed envelope in `message.content`; no GL selected |
| Remote fallback rejection | PASS | n/a | Dispatch blocked before transport; remote calls zero |

The successful probes did not require `message.thinking`. No instruct-profile
thinking anomaly was observed. The visual probe failed twice with the same
high-level defect class: it could not return schema-valid structured visual
content. No timeout occurred, but schema validation correctly rejected the
output.

Observed system RAM during successful probes ranged approximately from
16.4-16.7 GiB used. The successful text-only probes did not load measurable GPU
memory in the sampled post-response metric. The failed visual request took
approximately 56 seconds; no provider body was persisted in Git-facing output.

## Stop decision

Phase 3 required all four synthetic probes to pass before a private document
could be processed. The high-contrast visual probe failed, so the evaluation
stopped before:

- opening one frozen real document;
- the mandatory five-document gate;
- the ten-document gate;
- the 100-document baseline;
- simulated user learning or holdout evaluation.

No prompts, schemas, reconciliation rules, provenance requirements, readiness
rules, or thresholds were weakened after observing the failure. No additional
model was tested.

## Safety results

- private documents opened for instruct evaluation: 0
- private facts transmitted externally: 0
- remote provider calls: 0
- remote provider spend: USD 0.00
- source mutations: 0
- external fallback: 0
- model weights tracked: 0
- private responses/runtime telemetry tracked: 0
- final GL selected by the local model: 0
- export authorization performed by the local model: 0

Because no real-document gate ran, extraction completeness, provenance
coverage, reconciliation, row count, invoice count, readiness distribution,
and false-safe export rate cannot be claimed for this instruct candidate.

## Recommendation

Do not run the frozen sample or enable this instruct profile in production on
the current hardware. It is fast and well-behaved for text/candidate-only JSON,
but it cannot reliably produce structured visual output even for a simple
high-contrast synthetic invoice. Per the final-candidate instruction, do not
continue prompt tuning or test another model in this experiment.

**Final classification: LOCAL INSTRUCT RUNTIME NOT FEASIBLE**
