# Cost-optimized AI routing baseline

## Invariants

- Deterministic vendor processors and complete payable GL decisions make zero AI calls.
- Digital PDFs use embedded text before any visual route.
- Scans, photos, handwriting, structurally incomplete text extraction, or explicit operator requests may use Vision.
- All unresolved lines in one invoice are sent in one semantic request; the model returns candidates only.
- `AccountingDecisionEngine` remains the only component allowed to select the final GL.
- `AccountingReadiness` remains the only authority for readiness and export.
- Provider-specific profiles receive traffic only after real capability probes report them healthy.
- Credentials, request headers, raw provider errors, and private document contents are not written to public traces.

## Route order

1. Known deterministic parser: no AI.
2. Local text/PDF extraction and universal accounting rules: no AI.
3. Cheapest probe-verified text profile for incomplete digital documents.
4. Cheapest probe-verified multimodal profile for visual evidence that text cannot recover.
5. Cheapest probe-verified accounting profile for one grouped candidate-only request when no safe GL exists.
6. Independent verification only for configured exception policy; it is never treated as independent-family voting unless the capability report proves a different family.
7. Stronger/more expensive profiles remain explicit escalation profiles, not defaults.

## Provider contracts

- Gemini and DeepSeek use their configured OpenAI-compatible endpoints.
- Claude uses the native Messages API (`/v1/messages`) with the required API-version header and native image blocks.
- Model IDs are deployment configuration. The repository does not guess or silently substitute an unavailable model.
- Every logical role has a distinct profile ID, trace namespace, cache namespace, prompt, and capability report even when roles share a provider.

## Private activation sequence

1. Store keys only as `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, and `ANTHROPIC_API_KEY` in `.env`.
2. Configure explicit role model IDs documented in `.env.example`.
3. Run `python scripts/validate_provider_capabilities.py --list-only`; this makes zero provider calls.
4. Probe only the intended four profiles with repeated `--profile-id` arguments and write the full report under a private path.
5. Set `AI_PROVIDER_CAPABILITY_REPORT` to that private report.
6. Restart the backend. Cost routing then considers only healthy profiles and chooses the lowest configured token rates unless an explicit healthy profile ID overrides it.

## Current migration note

Legacy `runtime-*` OpenAI profiles remain eligible for backward compatibility. Provider-family profiles are fail-closed until named credentials, explicit models, and successful probes are all present. Unrecognized free-form credential lines are ignored and no credential value may be interpreted outside the narrowly allow-listed private aliases documented below.

## Validated local deployment (2026-07-15)

The private capability report activates these logical roles without committing
credentials or private probe evidence:

- `gemini-text`: routine digital-PDF structured extraction.
- `gemini-vision`: scan, photo, handwriting, and structurally incomplete-text escalation.
- `deepseek-accounting`: one grouped, candidate-only semantic request for unresolved invoice lines.
- `anthropic-verification`: isolated exception verification; it is not a production GL selector.

All four profiles passed real schema-validated probes. The strong-reasoning
state remains `shadow`. The serving provider/model from each extraction is
recorded in private operational metadata so cost-routed traffic is not
misattributed to the legacy compatibility profile.

## Cost controls and caches

- Extraction cache keys include the logical routing profile, so providers and
  roles cannot reuse each other's results accidentally.
- Semantic cache keys include provider, profile, model, source facts, and
  document context.
- `AI_MAX_COST_PER_BATCH_USD` is a fail-closed estimated-spend ceiling shared by
  text, Vision, repair, fallback, and retry calls in one Process action.
- A cache hit reserves no new budget. Every real retry reserves again because
  the provider can bill it.
- A new explicit Process action resets the in-memory estimate for that batch;
  retries inside the action cannot reset it.
- `AI_MAX_SEMANTIC_COST_PER_INVOICE_USD` separately bounds the grouped
  candidate-only reasoning request.
- Estimated costs are telemetry and safety inputs, not invoices. Provider
  dashboards remain the source of truth for billed spend.

## Private `.env` compatibility

The documented and preferred credential syntax is `NAME=value`. The local
loader also recognizes the owner's three pre-existing labels (`Deepseek API`,
`Gemini API`, and `Claude API`) as a temporary adapter. Values remain in process
memory only and are never logged, serialized, copied into reports, or committed.
