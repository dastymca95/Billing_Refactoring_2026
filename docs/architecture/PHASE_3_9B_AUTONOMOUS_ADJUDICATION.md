# Phase 3.9B — Autonomous document adjudication

## Outcome

The human and assisted-human labeling workspaces are suspended. Phase 3.9B adds
analytical outputs only; it does not alter `selected_120_v1`, Reviewer 1 drafts,
Reviewer 2 state, export authorization, or benchmark gold labels.

The autonomous pipeline is:

```text
source + private metadata + catalogs
→ capability-selected extraction
→ isolated verification
→ deterministic field consensus
→ arithmetic and structural validation
→ property and economic-responsibility resolution
→ semantic/GL candidates
→ AccountingDecisionEngine
→ AccountingReadiness
→ machine_adjudicated | exception_required
```

## Contracts and policy

- Result contract: `autonomous-adjudication/1.0`.
- Threshold policy: `autonomous-adjudication-policy/1.0`.
- Allowed output states: `machine_proposed`, `machine_verified`,
  `machine_adjudicated`, and `exception_required`.
- Machine outputs always retain `gold_status=not_gold`.
- Strong accounting reasoning remains shadow-only.

Every extracted field carries value, field-specific confidence, page/region,
source type, extraction profile, and supporting text or visual summary. The
verification contract records confirmed, corrected, rejected, missing,
alternative, and conflict results without consuming a primary reasoning
narrative.

Consensus distinguishes exact agreement, normalized agreement, supported single
source, resolvable conflict, unresolved conflict, missing, and not applicable.
Minor normalization differences do not create exceptions. Material unresolved
facts do.

Validation covers document totals, line sums, quantity × unit price, due versus
paid amounts, credit sign, service periods, page continuity, duplicate pages,
repeated-line flags, and allocation percentage totals. A failed material check
cannot be overridden by model confidence.

Property resolution accepts exact and normalized evidence plus injected
configured aliases. Conflicting candidates fail closed. Filename evidence may
support automatic selection when independently corroborated, but remains
non-authoritative alone below the configured threshold.

GL codes enter rows only when selected by AccountingDecisionEngine and when the
decision confidence reaches the GL threshold. AccountingReadiness remains the
only readiness/export authority.

## Runtime capability audit

The local runtime advertised no text-extraction, Vision, or accounting-reasoning
models. Configured names and credentials were not treated as availability.
Consequently, the reproducible private run used deterministic extraction plus an
independent structural/evidence verification pass. It did not pretend that
multimodal or handwriting verification occurred.

When provider discovery advertises a capable model, the gateway contract receives
the discovered capabilities and an explicit `isolated_verification=true` flag.
At most two independent model calls are allowed by policy. Unadvertised models
are never invoked.

## Private pilot run

- Documents analyzed: 20.
- `machine_adjudicated`: 0.
- `exception_required`: 20.
- Machine gold labels: 0.
- Human labels modified: 0.
- Reviewer 2 started: no.
- Strong-reasoner executions: 0.
- Advertised model capabilities: 0.

The dominant blockers were unresolved economic responsibility, missing required
financial facts/property/GL, and five arithmetic mismatches. These results are
correct fail-closed behavior for the currently available deterministic evidence;
they are not evidence that autonomous multimodal extraction meets product goals.

Detailed document outputs and exception counts remain exclusively below
`INNER_VIEW_PRIVATE_BENCHMARK_ROOT/analysis` and its private reports directory.
No IDs, filenames, paths, vendors, properties, raw text, or private decisions are
included in this Git-safe document.

## Remaining authorization gate

A runtime with an actually advertised Vision-capable extraction profile must be
configured and independently validated before expecting scans, photos,
handwriting, or incomplete deterministic documents to become machine-adjudicated.
No model is promoted automatically when that capability becomes available.
