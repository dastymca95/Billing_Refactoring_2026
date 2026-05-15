# Phase AI-2 - AI Vendor and GL Mapping Review Workflow

Date: 2026-05-10

## 1. Vendor Candidate Matching

Implemented a backend review service in `webapp/backend/services/ai_mapping_review.py` for AI-assisted invoices that require operator confirmation before their extracted values are trusted.

Vendor candidate behavior:

- Loads candidates from `Vendors/Vendor List.csv` without modifying the source vendor file.
- Normalizes detected names by lowering case, stripping punctuation, collapsing whitespace, and removing common business suffixes.
- Scores exact normalized matches, partial/token matches, fuzzy similarity, alias-style text overlap, and previously confirmed learned mappings.
- Returns top candidates with `vendor_name`, optional `vendor_id`, `score`, `reason`, and `source`.
- Keeps `needs_confirmation=true` by default so ambiguous AI vendor suggestions are operator-reviewable instead of silently accepted.

Example smoke coverage:

- `Lowe's Pro Supply` returns a strong Vendor List candidate.
- Future runs get a learned-mapping boost after the operator saves a confirmation.

## 2. GL Candidate Matching

Implemented GL candidate generation against `config/general_ledger_reference.yaml`.

GL candidate behavior:

- Validates all accepted GL accounts against the GL reference.
- Scores AI-suggested GL values when they match a real account.
- Scores line-item description keywords against GL account names and descriptions.
- Boosts likely expense accounts for supplier line items.
- Adds learned historical mapping boosts for vendor + item-pattern matches.
- Returns top candidates with `gl_account`, `gl_name`, `score`, `reason`, and `source`.

Smoke coverage includes a Lowe's-style hardware item, where `6651 Hardware - Misc` is selected as the strongest candidate.

## 3. Review UI

Added a compact AI mapping review workflow inside the Template workspace.

When AI-generated rows include unresolved mapping flags, the Template panel now shows:

- Detected vendor.
- Possible ResMan vendor candidates with scores and reasons.
- Manual vendor search.
- Save vendor mapping for future checkbox.
- Apply selected vendor to the current invoice.
- GL review cards for line items with unverified GL mappings.
- Top GL candidates with scores and reasons.
- Save GL mapping for future checkbox.
- Apply GL selection to similar line items option.

After an accepted mapping:

- The affected template rows are updated.
- Manual review reasons are reduced when the specific issue is resolved.
- Row metadata records provenance such as `Vendor mapping confirmed by user` or `GL mapping confirmed by user`.
- The preview, manual review list, and revisions are refreshed in the UI.

## 4. Learned Mappings Storage

Added `config/ai_learned_mappings.yaml`.

Structure:

```yaml
vendor_mappings: {}
gl_mappings: {}
```

Storage safeguards:

- No secrets are stored.
- Writes are atomic.
- A `.bak` backup is created before overwriting an existing mappings file.
- Vendor and GL values are validated before saving.
- Learned mappings are separate from deterministic vendor YAMLs and do not rewrite Python source.

## 5. Future Processing Integration

Integrated learned mappings into the AI invoice processing path only.

Flow:

1. AI extracts structured invoice data.
2. Backend validates and normalizes the AI result.
3. Backend checks learned vendor mappings.
4. Backend checks learned GL mappings per line item.
5. Confirmed mappings are applied to normalized output.
6. Resolved manual review reasons are removed when appropriate.
7. Mapping provenance is stored in row metadata.

Deterministic vendors such as Richmond Utilities and HWEA remain routed through their existing deterministic processors.

## 6. Backend/API Changes

Added `webapp/backend/api/ai_mappings.py`.

New routes:

- `GET /api/ai-review/vendor-candidates`
- `GET /api/ai-review/gl-candidates`
- `GET /api/ai-review/learned-mappings`
- `POST /api/batches/{batch_id}/ai-review/vendor-mapping`
- `POST /api/batches/{batch_id}/ai-review/gl-mapping`

The route contract verifier now checks these routes.

The apply endpoints update `_webapp_result.json`, mirrored vendor-group rows, manual review metadata, and the current revision snapshot when present.

## 7. Frontend Changes

Updated:

- `webapp/frontend/src/api.ts`
- `webapp/frontend/src/types.ts`
- `webapp/frontend/src/App.tsx`
- `webapp/frontend/src/components/TemplateWorkspace.tsx`
- `webapp/frontend/src/styles.css`
- `webapp/frontend/e2e/operator-visual.spec.ts`

The e2e fixture selection was hardened so general UI tests avoid transient AI smoke-test batches unless no stable batch exists.

## 8. Tests Performed

Frontend:

- `npm.cmd run build` - passed.
- `npx.cmd tsc --noEmit` - passed.
- `npm.cmd run test:e2e` - passed, 18 passed and 2 skipped because optional local fixture scenarios were unavailable.

Backend:

- `python -m compileall webapp\backend` - passed.
- `python scripts\verify_backend_routes.py` - passed.
- `python scripts\smoke_ai_openai_compatible_provider.py` - passed.
- `python scripts\smoke_ai_mapping_review.py` - passed.

Smoke coverage:

- Vendor candidate generation for Lowe's Pro Supply.
- Accept vendor mapping.
- Future invoice candidate boost from learned vendor mapping.
- GL candidate generation for known hardware line item.
- Accept GL mapping.
- Future similar item applies learned GL.
- Invalid GL rejected.
- Learned mappings file validates.
- Future normalized AI result applies learned vendor and GL mappings.
- Apply vendor mapping endpoint updates batch result.
- Apply GL mapping endpoint updates batch result.
- `/api/ai/status` does not expose API keys.

Integrity:

- `Output/Template.xlsx` unchanged.
- `Training Bills_Invoices` unchanged.
- `.env` unchanged.
- `Vendors` source files unchanged.
- No Dropbox calls were made.
- No real AI provider call was added to automated tests.

## 9. Limitations

- The review panel currently appears inside the Template workspace when AI mapping flags exist; there is no separate full-screen AI review queue yet.
- Vendor matching is strong enough for Phase AI-2, but future phases should add alias management UI and confidence threshold tuning.
- GL matching uses keyword, reference, AI-suggested, and learned mapping signals; it does not yet include a rich semantic embedding search.
- Learned mappings are file-based YAML, suitable for local/operator workflow; multi-user concurrency would need a database-backed lock/version model.
- Applying a mapping updates the current batch result and current revision snapshot, but does not export automatically.

## 10. Next Recommended Phase

Recommended Phase AI-3:

- Add a dedicated AI Review drawer or queue.
- Add alias management for vendor names.
- Add GL keyword pattern editor.
- Add provenance popovers on AI-generated cells.
- Add batch-level “review complete” status after all AI mapping reasons are resolved.
- Add more end-to-end tests using a deterministic AI fixture batch with unresolved mappings.
