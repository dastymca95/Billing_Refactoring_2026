# Phase AI-1.2 - Real AI Provider Connection + Manual Test Mode

Date: 2026-05-10  
Project: Billing Refactoring Web Console  
Stack target: backend `http://localhost:8001`, frontend `http://localhost:5174`

## 1. Scope

Phase AI-1.2 adds an explicit real-provider path for OpenAI-compatible invoice extraction APIs while keeping AI disabled by default and preserving the mock provider from Phase AI-1.1.

No real provider call was made during automated testing. Richmond Utilities and Hopkinsville Water/HWEA deterministic processors remain unchanged and continue to bypass AI.

## 2. Provider Abstraction Changes

Updated `webapp/backend/services/ai_provider.py`.

Supported provider modes now include:

- `AI_PROVIDER=mock`
- `AI_PROVIDER=openai_compatible`
- legacy `AI_PROVIDER=openai` remains supported with the default OpenAI-style base URL behavior

`openai_compatible` requires:

```env
AI_ASSIST_ENABLED=true
AI_PROVIDER=openai_compatible
AI_BASE_URL=<provider base URL>
AI_MODEL=<model name>
AI_API_KEY=<secret key>
```

The provider builds a `POST <AI_BASE_URL>/chat/completions` request unless the base URL already ends in `/chat/completions`.

No DeepSeek-specific base URL or model name was hardcoded. DeepSeek-compatible endpoints can be used only by supplying provider-specific values in `.env`.

## 3. Strict JSON Handling

The provider requests strict JSON with:

- system instruction: JSON only
- `response_format: {"type": "json_object"}`
- deterministic temperature `0`
- bounded `max_tokens`

Response handling now:

- accepts plain JSON
- extracts JSON from fenced code blocks
- extracts the first object if the provider wraps JSON in stray text
- rejects malformed JSON
- rejects non-object JSON
- validates required invoice schema keys
- validates `line_items` is a list of objects
- validates `warnings` is a list

New safe provider error classes:

- `AIProviderInvalidJSON`
- `AIProviderInvalidSchema`
- `AIProviderUnavailable`

When provider JSON/schema is invalid during batch processing, the file becomes manual review with:

- `ai_response_invalid_json`

Provider outages/timeouts remain:

- `ai_processing_failed`

## 4. Prompt Template

The backend prompt template remains in `ai_provider._build_prompt`.

It instructs the model to:

- return JSON only
- preserve vendor names as shown
- extract invoice number/date/due date/account/address/total
- extract variable supplier line items
- provide candidate GL mapping only
- use confidence scores and warnings
- use empty strings/nulls for unknowns
- not invent missing values

The prompt includes:

- strict output schema
- ResMan template columns
- vendor reference sample
- property reference sample
- general ledger reference sample
- truncated document text

## 5. Manual Test Endpoint

Added:

`POST /api/ai-invoice/test-extract`

Request:

```json
{
  "vendor_hint": "HD Supply",
  "document_text": "invoice text here",
  "dry_run": true
}
```

Behavior:

- requires `AI_ASSIST_ENABLED=true`
- requires a configured provider
- calls the configured provider
- parses and validates structured invoice JSON
- returns extraction, normalized validation summary, and manual-review reasons
- does not create a batch
- does not write `Output/Template.xlsx`
- does not create revisions
- does not trigger Dropbox
- does not persist source files

If `dry_run=false`, the endpoint returns `400`; this phase intentionally supports dry-run only.

## 6. Frontend AI Status

Updated `webapp/frontend/src/components/AiFallbackStatusBadge.tsx`.

Operator-visible states:

- `AI Off`
- `AI: Mock`
- `AI: Configured`
- `AI Error`

When configured with `openai_compatible`, the popover shows:

- Provider: `OpenAI-compatible`
- Model: configured model name

API keys are never displayed.

## 7. Safety and Cost Controls

Existing and added guardrails:

- AI remains disabled by default.
- Real provider calls require `AI_ASSIST_ENABLED=true`.
- `openai_compatible` requires base URL, model, and API key.
- No provider key is returned by `/api/ai/status`.
- `AI_TIMEOUT_SECONDS` caps provider runtime.
- `AI_MAX_TEXT_CHARS` caps prompt input.
- `AI_MAX_RESPONSE_TOKENS` caps provider response generation.
- `AI_MAX_OUTPUT_CHARS` caps response parsing.
- `AI_MAX_PAGES` caps text extraction from PDFs.
- Oversized document text is truncated safely and adds `ai_input_truncated` to warnings.
- Provider errors never crash a batch; they become manual-review reasons.

## 8. Tests Added

Added:

`scripts/smoke_ai_openai_compatible_provider.py`

The script monkeypatches `urllib.request.urlopen` inside `ai_provider`, so no real network request is made.

Coverage:

- AI disabled path
- missing API key
- missing model
- missing base URL
- no API key exposed in `/api/ai/status`
- manual dry-run endpoint with fake OpenAI-compatible response
- fenced JSON parsing
- malformed provider response rejection
- provider unavailable handling
- mock provider still configures

Updated:

- `scripts/verify_backend_routes.py` now requires `POST /api/ai-invoice/test-extract`.
- `scripts/smoke_ai_mock_provider.py` accepts the more specific invalid-response reason for malformed mock JSON.

## 9. Tests Performed

Frontend:

```powershell
cd webapp/frontend
npm.cmd run build
npx.cmd tsc --noEmit
npm.cmd run test:e2e
```

Backend:

```powershell
python -m compileall webapp\backend
python scripts\verify_backend_routes.py
python scripts\smoke_ai_mock_provider.py
python scripts\smoke_ai_openai_compatible_provider.py
```

Observed results:

- build passed
- TypeScript passed
- Playwright passed: 18 passed / 2 skipped
- backend compile passed
- route verifier passed
- mock provider smoke passed
- OpenAI-compatible smoke passed

OpenAI-compatible smoke summary:

```text
Provider disabled/missing config checks: OK
Manual test endpoint with fake provider: OK
Malformed provider JSON rejected: OK
Provider unavailable handling: OK
Mock provider still configures: OK
Phase AI-1.2 OpenAI-compatible provider smoke OK
```

## 10. Manual Enablement Instructions

Create or edit project-root `.env`:

```env
AI_ASSIST_ENABLED=true
AI_PROVIDER=openai_compatible
AI_BASE_URL=<your provider base URL>
AI_MODEL=<your model name>
AI_API_KEY=<your API key>
```

Restart the backend.

Check status:

```powershell
Invoke-RestMethod http://localhost:8001/api/ai/status
```

Dry-run provider extraction:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8001/api/ai-invoice/test-extract `
  -ContentType "application/json" `
  -Body '{"vendor_hint":"HD Supply","document_text":"Invoice text to test extraction","dry_run":true}'
```

Do not add real API keys to source control.

## 11. Limitations

- No real provider/key was manually tested in this phase.
- This phase does not claim DeepSeek or any specific provider works in the user environment; it adds the OpenAI-compatible transport and safe test endpoint.
- Vision/image payload support remains deferred.
- Real provider output quality still needs operator review on variable supplier invoice samples.
- The dry-run endpoint is developer/admin oriented and not yet surfaced as a full UI panel.

## 12. Confidence Handling Follow-Up

Follow-up improvement completed after the initial Phase AI-1.2 report:

- Top-level `confidence` is now backend-derived when the provider omits it or returns `0`.
- Line-item `confidence` and `reason` are now backend-derived when omitted.
- `needs_manual_review=true` from the model no longer creates a generic review reason by itself; backend validation creates specific operator-readable reasons.
- `manual_review_reasons` are now human-readable messages.
- Technical codes are preserved separately as `manual_review_codes` / `ai_validation_flags`.
- `validation_summary` now reports `total_reconciliation_passed`, `required_fields_present`, `line_item_count`, `dates_valid`, `confidence`, and `confidence_source`.
- The prompt now explicitly requires confidence values and line-item reasons.

Real dry-run retest with a synthetic Lowe's sample and configured `openai_compatible` provider returned:

- HTTP `200`
- extracted invoice fields
- provider confidence `0.8`
- `total_reconciliation_passed: true`
- human-readable review reasons for missing service/property data, unverified GL mapping, and vendor-reference mapping
- line-item confidence/reason values present

Additional smoke coverage was added to `scripts/smoke_ai_openai_compatible_provider.py` for a Lowe's-style response that omits top-level confidence and line-item confidence. The backend derives non-zero confidence and keeps property/GL review reasons clear.

## 13. Next Recommended Phase

Phase AI-1.3 should use a user-provided real provider key in a controlled manual session, run `/api/ai-invoice/test-extract` against one redacted HD Supply/Lowe's/Home Depot invoice text sample, capture the exact validation flags, and only then enable one QA batch run with Dropbox disabled.
