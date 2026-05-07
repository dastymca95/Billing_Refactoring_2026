# Phase 1Z — Vendor Rules Studio

**Date:** 2026-05-03
**Scope:** Add an in-app editor for vendor YAML rules without touching CLI processors, business logic, or vendor data.

---

## 1. Current rule architecture

The CLI processors at
`Training Bills_Invoices/Water - Sewer/<vendor>/process_<vendor>.py` are
already **YAML-driven**. Each one calls `yaml.safe_load()` exactly once on
`config/vendors/<vendor_key>.yaml` at startup and delegates everything
vendor-specific to that dict. Shared utilities (`utils/service_period_resolver.py`,
`utils/location_validator.py`, `utils/dropbox_uploader.py`) accept their
config as plain `dict` arguments — none of them have hardcoded vendor
rules.

This means the studio can edit YAML in place and the CLI picks up the
new values on the next run **without any code change in the processors**.

## 2. YAML vs Python — findings

| Topic | Hopkinsville | Richmond |
|---|---|---|
| Vendor identity | YAML (`vendor_identity`) | YAML |
| Invoice number format | YAML (`invoice_number_rules.format`) | YAML |
| Invoice / accounting / due date | YAML (`invoice_date_rules`, `due_date_rules`) | YAML |
| Service period (5-level fallback) | YAML (`service_period_rules`) — handled by `service_period_resolver.py` | YAML |
| Service address extraction | YAML regex (`pdf_extraction_rules.service_address_*`) | YAML |
| Property/unit matching | YAML (`account_number_unit_matching_rules`) + reference CSV | YAML |
| GL mapping | YAML (`service_gl_mapping`) | YAML |
| Tax / fee allocation | YAML (`service_grouping_rules.tax_rules`) | YAML |
| Total reconciliation | YAML (`amount_rules.tolerance`) | YAML |
| PDF extraction patterns | YAML (regexes) | YAML |
| Late / disconnect notices | YAML (`document_type_detection`, `disconnection_notice_extraction_rules`) | n/a |
| Dropbox / support documents | YAML (`support_document_rules`, `dropbox_rules`) — Python uploader reads env vars only | YAML |
| Manual review triggers | YAML map of booleans | YAML |

**Hardcoded in Python (today):** none of the above. The only behavior
not in YAML is the regex *engines* themselves, atomic file writing, and
secret loading from env (Dropbox tokens). All of these are intentionally
hardcoded — they don't belong in YAML.

**Safe to move to YAML in this phase:** nothing else needed; everything
the studio needs already lives in YAML.

**Risky to move:** PDF regex patterns. They're already in YAML but I
deliberately marked them **read-only** in the studio (Section 4 below)
because a malformed regex would break extraction silently.

## 3. Backend API — added in Phase 1Z

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/vendor-rules` | List vendors the studio can edit (HWEA, Richmond). |
| `GET` | `/api/vendor-rules/{vendor_key}` | Return rules in UI-friendly groups + current values + read-only sections. |
| `POST` | `/api/vendor-rules/{vendor_key}/validate` | Dry-run a patch. Returns `{ok, issues[]}`. |
| `PATCH` | `/api/vendor-rules/{vendor_key}` | Validate → backup → atomic write. |
| `POST` | `/api/vendor-rules/{vendor_key}/restore` | Restore from the most recent backup. |

Implementation:
- Service: [`webapp/backend/services/vendor_rules.py`](../../../webapp/backend/services/vendor_rules.py)
- Routes: [`webapp/backend/api/vendor_rules.py`](../../../webapp/backend/api/vendor_rules.py)
- Wiring: `webapp/backend/main.py` includes the router.

**Safety:**
- `vendor_key` must be lowercase `[a-z0-9_]+` and present in the editable whitelist.
- Resolved YAML path is `relative_to(config/vendors)` — path traversal is rejected.
- Patch fields are checked against `EDITABLE_PREFIXES`; non-whitelisted paths return a friendly issue.
- Unknown YAML keys outside the whitelist are **preserved untouched** (we round-trip the entire dict).
- Pre-write backup → `config/vendors/backups/<vendor_key>_<UTC>.yaml`.
- Atomic write: `tempfile.mkstemp()` in the same directory, then `Path.replace()`.
- No absolute paths leak in API responses.

## 4. Editable rule groups (Hopkinsville + Richmond)

Editable in this phase:

1. **Vendor Identity** — display name, active flag, aliases, detection keywords, accepted file types.
2. **Invoice Number** — format template, month case, year format, final-bill suffix.
3. **Dates** — invoice / due date fallback strategies + due-date offset days.
4. **Service Period** — output format, vendor-default-fallback (enabled / start offset / end offset / day-of-month), late-notice handling.
5. **GL Account Mapping** — per service group → ResMan GL code (validated as 3-6 digits).
6. **Total Reconciliation** — tolerance, rounding decimals.
7. **Support Documents** — Dropbox upload enabled, multi-bill PDF split enabled.
8. **Manual Review Triggers** — per-trigger boolean toggle.

Read-only in this phase (surfaced with a "Currently controlled by processor code. Not editable yet." panel):

- `pdf_extraction_rules` — regex patterns; one bad regex would break a whole vendor.
- `property_address_overrides`, `account_property_unit_mapping` — large reference data, edited via CSVs.
- `disconnection_notice_extraction_rules` (HWEA).
- `change_log`.

## 5. Save / backup behavior

1. Frontend sends `PATCH /api/vendor-rules/<key>` with `{patch: {dotted.path: value, ...}}`.
2. Backend revalidates the patch (same code as `/validate`).
3. Backend reads current YAML, applies the patch into the loaded dict.
4. Backend copies the **current YAML file** to `config/vendors/backups/<key>_<UTC>.yaml`.
5. Backend serializes the dict (`yaml.safe_dump`, `sort_keys=False`, block style, width=100).
6. Backend writes a temp file in the same directory and `Path.replace()`s it onto the target — atomic on Windows.
7. Backend re-reads the canonical file and returns the fresh `groups` payload to the UI.
8. Future CLI processor runs read the new YAML automatically.

`POST /restore` finds the newest backup matching `<key>_*.yaml` and overwrites the target file with it.

## 6. Validation behavior (friendly errors)

`validate_patch` in `vendor_rules.py` runs schema-level checks. Examples surfaced to the operator:

- "Display name cannot be empty."
- "GL account must be a 3-6 digit ResMan GL code."
- "Offset days must be between 0 and 90."
- "Unbalanced { } in invoice format."
- "'.PDF' is not a valid file type. Use lowercase, no dot (e.g. pdf, csv)."
- "This field is not editable in the studio: <path>." (when an unknown / locked key is sent.)
- "Trigger must be true or false."
- "Patch must be a non-empty mapping."

The `/validate` route returns 200 with `{ok: false, issues: [...]}` so the UI can highlight each field in place.

## 7. UI navigation changes

`webapp/frontend/src/components/NavRail.tsx`:
- Old: only `Batches`. Other former icons (Review/Vendors/Exports/Settings) had already been removed; only their unused SVG components remained in the file.
- New: `Batches` + `Rules`. Removed all dead-code icon functions.
- The rail now accepts `active` + `onSelect` props and routes between top-level modules.

`webapp/frontend/src/App.tsx`:
- Added `activeModule` state (`"batches" | "rules"`).
- When `activeModule === "rules"` the new `<VendorRulesStudio />` renders inside the layout. The original batch workspace JSX stays mounted but is hidden via CSS (`.layout.module-rules ... { display: none }`) so a quick toggle back doesn't lose batch state.

`webapp/frontend/src/components/VendorRulesStudio.tsx` (new):
- 3-pane layout — left: vendor list with status badge + last-updated stamp; center: collapsible group cards with inline fields; right: help panel that auto-updates with the focused field's description, example, and YAML path.
- Toolbar: `Reset`, `Validate`, `Restore`, `Save` + an unsaved-changes pill.
- Field inputs: `string` (text), `boolean` (checkbox), `integer/number` (number input), `enum` (select), `string_list` (textarea, one item per line).
- Inline validation errors per field; group headers carry a count of issues inside.
- Read-only fields render disabled with a "read-only" pill and explain why in the help panel.

Help-panel copy is **human-readable, not YAML-shaped** — for example the service-period fallback description reads "When the bill does not show reading dates, infer the service period as: Start invoice date minus N months, End invoice date minus M months, Day D" rather than `service_period_rules.vendor_default_fallback.start_offset_months = -1`.

## 8. Hopkinsville priority (PART F)

All 8 editable groups apply to Hopkinsville. Specifically:
- Regular bill PDF extraction → exposed read-only (regex patterns).
- Late notice / disconnect notice handling → editable strategy field under Service Period.
- Service address extraction → read-only in this phase.
- Property/unit matching → covered by reference data (CSVs), surfaced as read-only summary with key counts.
- Strict location validation → governed by reference CSV (Unit Info Clean.csv); behavior YAML lives under `location_validation_rules` (currently surfaced via the read-only path, scheduled for editable wiring in a follow-up phase).
- Invoice number format → editable.
- Due date logic → editable (strategy + offset).
- Service period logic → editable (output format + 4 fallback fields + late-notice handling).
- GL mapping → editable (per service group; GL code validated).
- Total reconciliation → editable (tolerance + rounding).
- Manual review triggers → editable (every boolean is a row).

## 9. Tests performed

- Frontend build: `cd webapp/frontend && npm run build` → ✓ 67 modules, 245.86 kB JS / 84.76 kB CSS.
- Backend compile: `python -m compileall webapp/backend -q` → no errors.
- Backend smoke (`scripts`-style inline run): see Section 10.
- Integrity:
  - `Output/Template.xlsx` — unchanged (not touched by any new code).
  - `Vendors/Vendor List.csv` — unchanged.
  - `.env` — unchanged.
  - No AI calls (no `ai_fallback` import touched).
  - No Dropbox calls (uploader isn't called from the studio path).

## 10. Backend smoke results

```
=== Phase 1Z smoke test ===
list: ['hopkinsville_water_environment_authority', 'richmond_utilities']
HWEA groups: ['vendor_identity', 'invoice_number_rules', 'date_rules',
              'service_period_rules', 'service_gl_mapping', 'amount_rules',
              'support_document_rules', 'manual_review_triggers',
              'pdf_extraction_rules', 'property_address_overrides',
              'account_property_unit_mapping',
              'disconnection_notice_extraction_rules', 'change_log']
Richmond groups: ['vendor_identity', 'invoice_number_rules', 'date_rules',
                  'service_period_rules', 'service_gl_mapping',
                  'amount_rules', 'support_document_rules',
                  'manual_review_triggers', 'pdf_extraction_rules',
                  'change_log']
validate-good: OK
bad-validate (tolerance out of range): Tolerance must be between 0 and 100.
bad-validate (GL code non-numeric):    GL account must be a 3-6 digit ResMan GL code.
bad-validate (offset out of range):    Offset days must be between 0 and 90.
bad-validate (unbalanced braces):      Unbalanced { } in invoice format.
bad-validate (non-editable section):   This field is not editable in the studio.
bad-validate (trigger non-bool):       Trigger must be true or false.
bad-validate (file type uppercase+dot): '.PDF' is not a valid file type.
bad-validate (empty patch):            Patch must be a non-empty mapping.
unknown-vendor: 400 Vendor '...' is not editable in this build.
traversal: 404
patch backup: hopkinsville_water_environment_authority_20260503T170606Z.yaml
restored OK; tolerance unchanged from baseline
YAML still valid + has 34 top-level sections
=== ALL OK ===
```

The smoke test creates a backup file then restores from it. The
generated `_20260503T170606Z.yaml` was deleted after the run so the
production backups directory stays clean.

## 11. Limitations

- Editable scope is intentionally narrow. Regex patterns, address overrides, and the 80-row `account_property_unit_mapping` for HWEA are surfaced read-only. Editing them in-app would require a richer UI (regex tester, CSV-style table) that wasn't in scope for Phase 1Z.
- "Restore" picks the *latest* backup only. There's no backup-history browser yet.
- The studio doesn't yet show a diff preview before save. Operators see the new state immediately after save instead.
- Manual review triggers are surfaced as a flat alphabetical list. Categorizing them into "data quality / amount / address / dates" groups is a follow-up.
- No advanced YAML view yet (the spec mentioned it as optional). Keeping the UI strictly form-based for now.

## 12. Recommended next phase

Phase 1Z+ (Vendor Rules Studio v2):
- Editable PDF regex patterns with a built-in tester ("paste sample text → see captured groups").
- Diff preview before save (left = current YAML, right = pending YAML, intra-line highlighting).
- Backup history browser + per-backup restore.
- Categorize manual-review triggers; bulk-toggle by category.
- "Try this rule against an existing batch" — reprocess in dry-run mode and show the delta in the manual-review count.
- Light Settings module: Dropbox status, AI fallback policy, log level — reusing the same form patterns.
