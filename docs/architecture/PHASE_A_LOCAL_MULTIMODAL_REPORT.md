# Phase A Local Multimodal Execution Report

Date: 2026-07-19  
Branch: `experiment/document-learning-simulation`  
Classification: **LOCAL RUNTIME NOT FEASIBLE**

## Scope and preservation

The experiment used the existing frozen Phase A selection without modifying
source documents or its leakage-safe split. The selected sample contains 100
physical documents: 13 unique invoice units have defensible labels and 87 are
coverage-only. The accepted split contains six training, three similar
holdout, two unrelated holdout, one benchmark-only, and one rule-simulation
invoice unit. Holdout answers are not embedded in the split manifest.

The private corpus, experiment databases, rendered pages, OCR, model output,
telemetry, and model weights remain ignored or outside the repository. External
Phase A run count and provider spend remained zero. No commit or push was made.

## Hardware and selected runtime

- Windows 11
- AMD Ryzen 9 5900HX, 8 cores / 16 logical processors
- 23.37 GiB system RAM; approximately 8 GiB available at preflight
- NVIDIA RTX 3050 Ti Laptop GPU with 4 GiB VRAM
- CUDA-capable local Ollama execution
- 115.6 GiB free local storage at preflight
- Python 3.11.5
- Ollama 0.32.1 bound to `127.0.0.1:11434`
- `qwen3-vl:2b`, Q4-class 1.9 GB local model, 8,192-token runtime context
- Local PDF embedded-text extraction plus controlled local rendering as
  supporting evidence

The 2B model was selected because a larger verifier could not be loaded safely
within 4 GiB VRAM while preserving useful visual resolution. The Ollama server
was explicitly configured without cloud fallback and listened only on the
literal loopback address.

## Implemented local execution boundary

The local provider uses the existing extraction abstraction and returns a typed
candidate-only result with safe request identity, provider/model/profile,
page references, fields, line items, evidence, confidence, warnings, latency,
and local RAM/GPU measurements. It removes any provider-supplied final GL,
readiness, or export authorization.

Experiment-local isolation:

- ignores remote profile credentials and endpoints;
- permits only literal loopback endpoints;
- blocks hostname/DNS and non-loopback socket connections process-wide in the
  dedicated runner;
- records safe blocked-dispatch events without request bodies;
- has regression coverage proving that no remote fallback can execute.

Local text and visual invoice calls are facts-only. They do not receive GL
catalogs, property catalogs, vendor references, or tenant accounting policy.
Semantic candidates continue through the existing pipeline;
`AccountingDecisionEngine` remains the final GL authority and
`AccountingReadiness` remains the export authority.

## Capability observations

Synthetic, public-safe probes established that the runtime can perform basic
loopback inference:

- high-contrast visual text probe: exact result in 7.86 seconds;
- candidate-only accounting envelope: schema-valid result in 7.59 seconds;
- local vision profile verified text extraction, visual understanding,
  handwriting declaration, and structured output on bounded synthetic probes;
- remote provider calls: zero.

These probes do not establish private-document extraction quality.

## Calibration results and stop decision

The initial thinking-tag model returned schema-valid JSON under Ollama's
private `message.thinking` field while leaving `message.content` empty. The
adapter now accepts that field only when the complete value parses and validates
against the typed extraction contract. Free-form reasoning remains rejected and
is never serialized or logged.

One isolated real document passed the transport after that correction, with one
local call, zero remote calls, and a measured provider span of 5.764 seconds.
Its 278.560-second wall time was contaminated by two simultaneous local model
downloads and is not a valid throughput benchmark. The selected document was
coverage-only, so no accuracy claim is available.

The formal five-document gate then exhibited repeated invalid-schema retries,
one `done_reason=length` response containing non-JSON thinking, and did not
finish after more than eleven minutes. It was terminated fail-closed. No gate
of 10, complete shard, or 100-document run was started.

The demonstrated local text defect was then fixed: local text extraction now
uses the facts-only schema and rejects empty/non-reconciled invoice structure.
The required affected-case replay reached the 180-second local transport
timeout, returned `local_ollama_transport_unavailable`, produced no accepted
invoice, and made no remote call. Wall time was 227.017 seconds.

Therefore the current 2B thinking model plus 4 GiB GPU is not stable or fast
enough for a meaningful 100-document Phase A pilot. The experiment stop rule
for repeated instability/structurally unusable output applies.

## Metrics that may and may not be claimed

Established:

- private external transmissions: 0;
- remote provider calls: 0;
- remote provider spend: USD 0.00;
- source mutations: 0;
- private Git artifacts detected by the runner: 0;
- false-safe exports observed: 0;
- AccountingDecisionEngine/AccountingReadiness authority bypasses: 0.

Not established because the five-document gate did not pass:

- extraction field accuracy;
- reconciliation rate;
- provenance coverage across the sample;
- GL Top-1, Top-3, or MRR;
- handwriting performance;
- baseline versus approved-learning improvement;
- correction persistence across the planned UI simulation;
- negative-control results for the full Phase A workflow;
- local throughput for the 100-document sample.

No baseline, simulated-user learning, holdout reprocessing, governed-rule
activation, or Phase B/Phase C work was performed.

## Defects demonstrated and corrected

1. Remote fallback exposure in an experiment process: fixed with explicit
   local-only provider loading plus HTTP/socket fail-closed guards and tests.
2. Thinking-tag structured payload misplaced outside response content: accepted
   only after full typed validation; free-form thinking remains blocked.
3. Local visual prompt included accounting context: routed to facts-only
   extraction before semantic/accounting stages.
4. Local text prompt included catalogs and could return an unrelated structure:
   converted to facts-only and protected by invoice line/total validation.
5. Runtime failures could lose a safe type-specific reason: local failures now
   preserve a safe failure code without provider bodies or private text.

None of these changes lowers accounting, readiness, provenance, or export
thresholds.

## Recommendation

Do not run the frozen 100-document sample with `qwen3-vl:2b` thinking on this
4 GiB GPU. Before another authorization, benchmark a non-thinking instruct
vision tag on five documents, or use hardware with materially more VRAM. The
same 5 -> 10 -> 10 stop gates must remain in place. Phase 6 onward must remain
blocked until the five-document output is structurally usable, evidence-backed,
and resource-stable.

**Final classification: LOCAL RUNTIME NOT FEASIBLE**
