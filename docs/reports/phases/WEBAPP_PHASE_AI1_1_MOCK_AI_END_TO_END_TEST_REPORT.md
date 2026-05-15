# Phase AI-1.1 - Mock AI End-to-End Test for Variable Vendor Invoices

Date: 2026-05-10  
Project: Billing Refactoring Web Console  
Stack target: backend `http://localhost:8001`, frontend `http://localhost:5174`

## 1. Scope

This phase verifies the AI-assisted invoice pipeline without a real provider key or external network call. The test path uses a deterministic mock provider that returns realistic structured invoice JSON for a variable supplier invoice, then runs the same backend validation and ResMan preview mapping used by the AI-assisted path.

No vendor extraction business logic was changed. Richmond Utilities and Hopkinsville Water/HWEA remain deterministic and bypass the AI-assisted route.

## 2. Mock Provider Mode

Added backend mock mode:

```env
AI_ASSIST_ENABLED=true
AI_PROVIDER=mock
AI_MODEL=mock-invoice-v1
```

No `AI_API_KEY` or `AI_BASE_URL` is required for `AI_PROVIDER=mock`.

Optional test controls:

```env
AI_MOCK_MODE=malformed_json
AI_MOCK_DELAY_SECONDS=0
```

The mock provider lives in `webapp/backend/services/ai_provider.py` and returns a realistic HD Supply-style structured invoice:

- vendor: `HD Supply Facilities Maintenance, Ltd`
- invoice number: `HDS-104857`
- invoice/due dates
- account number and service address
- subtotal, tax, and total
- two line items with GL account candidates
- confidence scores
- warnings/manual-review flags when requested

Fault modes are deterministic:

- `MOCK_MALFORMED_JSON` or `AI_MOCK_MODE=malformed_json` rejects provider output.
- `MOCK_TOTAL_MISMATCH` creates a total reconciliation flag.
- `MOCK_LOW_CONFIDENCE` creates low-confidence row/cell flags.

## 3. Fixture

Added safe text-only fixture:

`webapp/backend/tests/fixtures/variable_supplier_mock_invoice.txt`

The fixture is copied into QA-created webapp batches by the smoke script. It does not modify source training files, source PDFs/CSVs, `Output/Template.xlsx`, Dropbox, or AI provider configuration.

## 4. End-to-End Backend Smoke

Added:

`scripts/smoke_ai_mock_provider.py`

The script uses FastAPI `TestClient` in-process and:

1. Enables `AI_PROVIDER=mock`.
2. Creates QA-only batches.
3. Uploads the safe variable supplier fixture.
4. Processes the batch through the normal `/api/batches/{batch_id}/process?sync=1` path.
5. Confirms unknown/variable invoice routing reaches the AI-assisted processor.
6. Confirms backend validation creates ResMan preview rows with AI provenance.
7. Deletes QA-only batches at the end of each case.

Cases covered:

- AI disabled path returns `ai_invoice_processing_not_configured` and does not crash.
- Mock provider status returns enabled/configured and does not expose API keys.
- Mock success path generates preview rows with `_meta.ai_generated`.
- Malformed mock JSON is rejected and becomes `ai_processing_failed`.
- Total mismatch creates `total_reconciliation_failed`.
- Low confidence creates `ai_confidence_low`.
- Richmond/HWEA deterministic vendor detection remains `processing_mode=deterministic`.

## 5. UI State

Updated the AI status pill so mock mode is visible to operators:

- Label: `AI: Mock`
- Message: mock provider is enabled and does not use external calls or API keys.

Added Playwright coverage by intercepting `/api/ai/status` and verifying the rendered pill text.

Added a second Playwright check that intercepts `process` and `progress` for an existing file batch, simulates an `ai_assisted` processing snapshot, and verifies the in-document scan overlay renders `Reading line items` for the active file. This does not call a provider or run vendor processors.

## 6. Validation Fix Found During Testing

The mock success path exposed a real integration bug: AI-generated rows were preserving `_meta.ai_generated`, but `row_normalizer` was overwriting the AI-extracted vendor name with the routing bucket key `ai_assisted`.

Fix:

- `webapp/backend/services/row_normalizer.py` now skips canonical vendor overwrite when `vendor_key == "ai_assisted"`.
- Result: AI rows keep the provider/validator vendor value, e.g. `HD Supply Facilities Maintenance, Ltd`.

## 7. E2E Hardening Fixes

While running the full Playwright suite, two existing E2E/UI contract issues appeared:

- Batches panel chrome measured `33px` against the compact `32px` window-header contract.
- File delete tests still expected an always-visible delete button, but the UI now correctly exposes delete inside the file kebab menu.

Fixes:

- Reduced `.file-sidebar-header` to an exact `32px`.
- Added optional `testId` support to `KebabMenu` items.
- Added `explorer-file-delete` to the Delete file menu item.
- Updated Playwright tests to open the file menu before choosing Delete file.

## 8. Tests Performed

Frontend:

```powershell
cd webapp/frontend
npm.cmd run build
npx.cmd tsc --noEmit
npm.cmd run test:e2e
```

Results:

- `npm.cmd run build`: passed.
- `npx.cmd tsc --noEmit`: passed.
- `npm.cmd run test:e2e`: passed, 18 passed / 2 skipped.

Backend:

```powershell
python -m compileall webapp\backend
python scripts\verify_backend_routes.py
python scripts\smoke_ai_mock_provider.py
```

Results:

- `compileall`: passed.
- route verifier: passed.
- mock AI smoke: passed.

Smoke output summary:

```text
AI disabled path: OK
Mock status endpoint: OK
Mock AI success path: OK
Malformed mock JSON rejected: OK
Total mismatch validation: OK
Low confidence validation: OK
Deterministic vendor routing: OK
Phase AI-1.1 mock provider smoke OK
```

The log line `AI invoice processing failed ... AI response was not valid JSON` is expected for the malformed JSON test case.

## 9. External Call Safety

No real AI provider was called.

The mock mode returns locally generated structured data and explicitly works without:

- `AI_API_KEY`
- `AI_BASE_URL`
- external model calls
- Dropbox uploads
- vendor CLI processor execution in the mock smoke test

## 10. Limitations

- The browser E2E verifies the visible `AI: Mock` state and verifies the scan overlay from a mocked AI progress snapshot.
- It does not yet drive a live long-running mock processing run from upload through final grid output in the browser.
- Vision/image payloads remain deferred; mock AI uses text-first fixture data.
- Real provider quality, cost, and timeout behavior still require a later provider-specific test pass.

## 11. Next Recommended Phase

Phase AI-1.2 should add a browser fixture flow that starts the backend with `AI_PROVIDER=mock` and a short `AI_MOCK_DELAY_SECONDS`, processes the variable supplier fixture from the UI, captures the AI scan overlay, and verifies low-confidence cell styling directly in the grid.
