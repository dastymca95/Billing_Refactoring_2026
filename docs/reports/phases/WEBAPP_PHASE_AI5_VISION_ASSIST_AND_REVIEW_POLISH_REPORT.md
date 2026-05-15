# Phase AI-5 — Vision-Assisted Invoice Extraction + Advanced Review Polish

Date: 2026-05-11

## 1. Vision Architecture

Phase AI-5 adds an opt-in vision layer on top of the existing AI-assisted variable invoice pipeline. Text extraction remains the primary path. Vision assist is provider-gated, disabled by default, and exposed through a dry-run endpoint for a single batch document:

- `POST /api/batches/{batch_id}/ai-invoice/vision-assist`

The route does not export, create revisions, trigger Dropbox, or modify source documents. It may write batch-local trace overlay metadata when a vision provider returns bounding boxes.

## 2. Provider Capability Detection

Added config:

```env
AI_VISION_ENABLED=false
AI_VISION_PROVIDER=openai_compatible
AI_VISION_MODEL=
AI_VISION_BASE_URL=
AI_VISION_API_KEY=
AI_VISION_MAX_PAGES=2
AI_VISION_MAX_IMAGE_WIDTH=1600
AI_VISION_MODE=fallback_only
```

`GET /api/ai/status` now returns:

- `supports_vision`
- `vision_enabled`
- `vision_model`
- `vision_mode`

For `openai_compatible`, no image is sent unless `AI_VISION_ENABLED=true`. Vision can use a separate provider/base URL/key via `AI_VISION_PROVIDER`, `AI_VISION_BASE_URL`, and `AI_VISION_API_KEY`; otherwise it falls back to the normal AI provider credentials. `AI_VISION_MODEL` must name a model that accepts image input. Mock mode can exercise the full vision path without external calls.

## 3. Vision Trigger Rules

Implemented automatic vision routing for screenshots/photos and weak-OCR PDFs. Uploaded PNG/JPG/WebP/GIF/BMP invoice screenshots go directly through the vision extraction call when vision is enabled. PDFs remain text-first unless OCR is empty/weak or `AI_VISION_MODE=always`.

## 4. Page Rendering

Added `webapp/backend/services/ai_vision.py`:

- renders selected PDF pages with local PyMuPDF when available
- caps pages with `AI_VISION_MAX_PAGES`
- caps rendered width with `AI_VISION_MAX_IMAGE_WIDTH`
- stores temporary PNGs only under the batch temp folder
- deletes temporary images after encoding
- returns friendly `Vision rendering unavailable.` if rendering support is missing

Filenames are normalized to basename before local reads.

## 5. Strict JSON Schema

Added `extract_invoice_vision_structured()` to `ai_provider.py`. It uses the OpenAI-compatible chat/completions shape with mixed text/image content and requires strict JSON. The schema includes invoice fields, line items, confidence, warnings, and optional `vision_candidates` bboxes.

Malformed JSON is rejected with a 422 response; provider/runtime failures return safe operator messages without secrets.

## 6. Text + Vision Merge

Added `merge_text_and_vision_results()`:

- keeps validated text extraction as primary
- fills blank important fields from vision candidates
- boosts confidence when text and vision agree
- flags `ai_text_vision_conflict` when important fields disagree
- preserves manual-review reasons and validation summary fields

## 7. Trace Overlay Integration

Vision candidate bboxes are saved to the same batch trace folder used by the document overlay:

- source type: `ai_vision`
- match strategy: candidate validation status
- dashed purple styling in the PDF trace overlay
- tooltip includes field/value/confidence through the existing trace UI

## 8. Single Invoice UI Polish

Single Invoice Mode now includes:

- `Use vision assist` button in the invoice titlebar
- compact review task summary collapsed by default
- disabled `Ready to export` tooltip explaining blockers
- `Accept property` action when a strong property candidate exists
- GL options for `Save mapping for future` and `Apply to similar items`
- direct tax actions: Distribute tax, Separate tax line, Leave for review
- compact vision notice after a vision assist run
- AI status popover shows vision status/model/mode

The field-level AI suggestion dropdown model from AI-4 remains intact; suggestions no longer accumulate in a global strip.

## 9. Tests Performed

Passed:

- `python -m compileall webapp\backend`
- `python scripts\verify_backend_routes.py`
- `python scripts\smoke_ai_vision_assist.py`
- `python scripts\smoke_ai_openai_compatible_provider.py`
- `python scripts\smoke_ai_mapping_review.py`
- `cd webapp/frontend && npx.cmd tsc --noEmit`
- `cd webapp/frontend && npm.cmd run build`
- `cd webapp/frontend && npm.cmd run test:e2e`

E2E result: 21 passed, 2 skipped because optional fixture data was unavailable.

The new smoke test verifies:

- AI disabled status
- explicitly enabled vision reports safe configuration without exposing keys
- vision disabled endpoint returns a friendly error
- mock vision success path returns trace bboxes
- malformed mock vision JSON is rejected
- text/vision agreement boosts confidence
- text/vision conflict adds manual-review flag

## 10. Limitations

- No real external vision provider was called in automated tests.
- Automatic vision triggering is enabled for uploaded screenshots/photos and weak-OCR PDFs when `AI_VISION_ENABLED=true`.
- PDF rendering depends on PyMuPDF availability. If unavailable, the endpoint returns a clear message.
- Vision candidates are trace/provenance aids; they do not overwrite confirmed template fields automatically.

## 11. Next Recommended Phase

Phase AI-6 should add operator-controlled accept/reject flows for individual vision candidates, then enable automatic fallback vision only for weak OCR or failed total reconciliation after real provider validation.
