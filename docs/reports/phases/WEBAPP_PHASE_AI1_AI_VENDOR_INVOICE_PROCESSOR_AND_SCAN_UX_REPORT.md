# Phase AI-1 — AI Vendor Invoice Processor + Scan UX

Date: 2026-05-10  
Project: Billing Refactoring Web Console  
Stack checked: backend `http://localhost:8001`, frontend `http://localhost:5174`

## 1. Architecture

Phase AI-1 adds an AI-assisted route for highly variable supplier invoices while preserving the deterministic processors for structured utility vendors. The new AI path is opt-in, disabled by default, and produces structured extraction candidates that the backend validates before rows reach the ResMan preview/export flow.

Existing deterministic processors remain the first route for Richmond Utilities, Hopkinsville Water Environment Authority, Columbia Power and Water, Atmos, Hardin County Water, Shelbyville Power, Zillow Rentals, McMinnville Electric, and Pennyrile Electric.

## 2. Provider Abstraction

Added `webapp/backend/services/ai_provider.py`.

Environment/config values:

```env
AI_ASSIST_ENABLED=false
AI_PROVIDER=
AI_MODEL=
AI_API_KEY=
AI_BASE_URL=
AI_TIMEOUT_SECONDS=45
AI_MAX_TEXT_CHARS=45000
AI_MAX_OUTPUT_CHARS=20000
AI_MAX_PAGES=5
```

The provider API is OpenAI-compatible for Phase AI-1 and is intentionally small:

```python
extract_invoice_structured(
    vendor_hint,
    document_text,
    page_images_or_refs,
    template_schema,
    property_reference,
    gl_reference,
    vendor_reference,
)
```

`AI_PROVIDER=openai` can use the default OpenAI-compatible base URL. Other providers, including DeepSeek-compatible APIs, require `AI_BASE_URL`. Gemini/Anthropic-specific adapters are deferred, but the service boundary supports adding them without changing the batch processor.

API keys are process-only and are never returned to the frontend.

## 3. Routing Logic

Updated `webapp/backend/services/batch_processor.py` so unsupported/unknown vendor groups can route to AI-assisted processing when appropriate.

Rules:

- Deterministic vendor key present in `_PROCESSOR_LOADERS` → unchanged deterministic processor.
- Variable supplier vendor or unknown vendor → AI-assisted route.
- AI disabled/not configured → no provider call; file becomes a clear manual-review item.
- Batch processing continues and does not crash.

Updated `webapp/backend/services/vendor_detection.py` with lightweight variable supplier hints for:

- `hd_supply`
- `lowes`
- `home_depot`

These are marked `processing_mode: ai_assisted`, not deterministic.

## 4. Extraction Schema

Added `webapp/backend/services/ai_invoice_processor.py`.

The AI provider is prompted to return this strict JSON object shape:

- invoice header fields: vendor, invoice number/date, due date, account, address, property candidate/abbreviation
- `line_items[]` with description, quantity, unit price, amount, GL candidate, expense type, reserve flag, confidence, reason
- subtotal/tax/shipping/fees/total
- confidence, warnings, manual review flag

Rows are mapped into the existing ResMan columns and carry `_meta.ai_*` provenance fields.

## 5. Validation Rules

Backend validation now checks:

- total reconciliation within `0.01`
- vendor name against `config/vendor_rules_index.yaml`
- property abbreviation against `Properties/Properties.csv` / `Unit Info Clean.csv` when possible
- GL candidate against `config/general_ledger_reference.yaml`
- date parseability
- numeric/rounded amounts
- low confidence and provider warnings

Important flags added:

- `ai_confidence_low`
- `total_reconciliation_failed`
- `vendor_mapping_not_found`
- `property_abbreviation_missing`
- `ambiguous_gl_mapping`
- `ai_processing_failed`
- `ai_invoice_processing_not_configured`

## 6. API Changes

Updated:

- `GET /api/ai/status`

Now returns:

```json
{
  "enabled": false,
  "provider": null,
  "model": null,
  "configured": false,
  "supports_vision": false,
  "message": "AI is not configured."
}
```

Added:

- `GET /api/ai/invoice/status`
- `POST /api/ai/invoice/validate`

The route verifier now includes both new AI invoice helper routes.

## 7. Frontend UX

Added `webapp/frontend/src/components/AiScanOverlay.tsx`.

The document viewer now accepts `aiProgress` and renders a polished scan overlay while AI-assisted processing is active:

- PDF remains visible.
- A subtle blue scan beam sweeps over the document surface.
- Progress card shows stage, file, and percent.
- Respects `prefers-reduced-motion`.

Stages surfaced through progress:

- Scanning invoice
- Reading line items
- Matching vendor
- Mapping GL accounts
- Validating totals
- Building ResMan template

Template row/cell styling was extended:

- AI-generated rows get a subtle blue wash.
- Low-confidence/validation issue cells get a soft warning fill.
- Cell explain responses include AI provenance, confidence, validation flags, and warnings.

## 8. Safety / Cost Controls

- AI is disabled by default.
- Provider calls occur only when `AI_ASSIST_ENABLED=true` and required settings are present.
- Text-first extraction only in this phase.
- Page/text/output limits are enforced.
- Provider timeout is enforced.
- Malformed provider JSON is rejected.
- Provider failures become manual-review reasons, not batch crashes.
- No AI keys are exposed in `/api/ai/status` or frontend state.

## 9. Browser / Screenshot Check

Screenshot captured:

- `docs/reports/phases/screenshots/phase_ai1/ai_status_disabled_app.png`

Observed:

- Live backend on `localhost:8001` returned AI disabled/config-safe status.
- App loaded at `localhost:5174`.
- AI provider secrets were not visible.

Limitations:

- No real provider call was made.
- The scan overlay was not exercised with a live AI provider because AI remains disabled by default.
- The browser-use Node REPL execution tool was not available in this session; Playwright CLI was used for the simple app screenshot.

## 10. Tests Performed

Frontend:

```powershell
cd webapp/frontend
npm.cmd run build
npx.cmd tsc --noEmit
```

Backend:

```powershell
python -m compileall webapp\backend
python scripts\verify_backend_routes.py
```

Smoke tests:

- `GET /api/ai/status` returns friendly disabled status.
- Status response does not expose `AI_API_KEY`.
- Malformed extraction payload is rejected by the Python validator.
- Malformed JSON to `/api/ai/invoice/validate` returns `422`.
- Total mismatch creates `total_reconciliation_failed`.
- Unknown vendor creates `vendor_mapping_not_found`.
- Missing property creates `property_abbreviation_missing`.
- Missing/unknown GL creates `ambiguous_gl_mapping`.
- QA-created unknown text invoice with AI disabled processes without crash and returns `ai_invoice_processing_not_configured`.
- Richmond/Hopkinsville processor files compile unchanged:
  - `Training Bills_Invoices\Water - Sewer\Richmond Utilities\process_richmond_utilities.py`
  - `Training Bills_Invoices\Water - Sewer\Hopkinsville Water Environment Authority\process_hopkinsville_water_environment_authority.py`

## 11. Enabling AI

1. Open or create project-root `.env`.
2. Add provider settings:

```env
AI_ASSIST_ENABLED=true
AI_PROVIDER=<provider-or-openai-compatible-name>
AI_MODEL=<provider-model-name>
AI_API_KEY=<your-key>
AI_BASE_URL=<provider-base-url-if-required>
```

3. Restart the FastAPI backend.
4. Refresh the frontend. Restart Vite only if frontend environment settings changed.
5. Confirm:

```powershell
Invoke-RestMethod http://localhost:8001/api/ai/status
```

## 12. Limitations

- Vision/image payloads are not sent yet; scanned invoices without text layers become manual-review unless a later provider-specific vision adapter is added.
- AI extraction quality depends on the configured provider/model and the OCR/text layer available.
- GL/property mapping is conservative; ambiguous results are flagged rather than forced.
- Browser automation did not run a real AI scanning pass because no provider/API key was configured.

## 13. Next Recommended Phase

Phase AI-2 should add a provider-mock mode and fixture-driven tests so the browser can show the scan overlay and generated rows without external cost. After that, add provider-specific vision support for scanned supplier invoices and a tighter operator review panel for AI provenance.
