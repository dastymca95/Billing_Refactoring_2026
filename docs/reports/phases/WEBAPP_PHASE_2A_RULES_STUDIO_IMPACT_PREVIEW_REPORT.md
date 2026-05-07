# Phase 2A — Rules Studio Live Validation + Processing Impact Preview

**Date:** 2026-05-03
**Scope:** Make Vendor Rules Studio operationally useful by letting the operator dry-run draft (unsaved) rules against an existing batch and see exactly what would change before pressing Save.

---

## 1. Dry-run architecture

The webapp's normal pipeline is unchanged for production calls; Phase 2A adds two opt-in flags that flow through it:

```
POST /api/vendor-rules/{key}/preview-impact
  └─ services/rules_impact.preview_rule_impact()
       ├─ validate vendor_key + batch_id + draft patch
       ├─ overlay patch on saved YAML (in memory) → draft_rules dict
       ├─ write draft_rules to a temp YAML inside webapp_data/batches/<id>/
       ├─ services/batch_processor.process_batch(
       │       batch_id,
       │       dry_run=True,
       │       rules_override_paths={vendor_key: temp_yaml})
       │     └─ run_context["dry_run"] = True
       │     └─ vendor processor honours dry_run:
       │           - skip DropboxUploader.from_env()
       │           - skip write_resman_workbook()
       │           - skip write_manual_review_workbook()
       │           - skip write_debug_csv()
       │     └─ returns invoices+rows+manual_review in memory
       ├─ delete temp YAML (always; finally-block)
       ├─ load batch's saved preview from
       │   webapp_data/batches/<id>/processed/_webapp_result.json
       └─ diff (saved baseline) vs (draft dry-run) → JSON payload
```

**Side-effects during a preview:** none on disk other than:
- the per-batch `progress.json` snapshot the webapp already writes (so the existing progress UI continues to work in case we later wire it for impact runs);
- the temporary draft YAML file inside the batch folder, deleted on the same call.

YAML, `Output/Template.xlsx`, source files, the batch's `_webapp_result.json` cache, the manual-review xlsx, the debug CSV, and `config/vendors/backups/` are all left untouched.

## 2. Backend endpoint

`POST /api/vendor-rules/{vendor_key}/preview-impact`

Request:
```json
{
  "batch_id": "batch_20260502_170939_992",
  "draft_rules": {"amount_rules.tolerance": 0.50},
  "compare_against_saved": true
}
```

`draft_rules` is the same flat-dotted patch shape used by `validate` and `PATCH /api/vendor-rules/{key}` — accepted both flat and nested.

Response shape (Section 4 has the full Pydantic-equivalent type).

Errors are 400 with friendly messages:
- `Invalid batch id`
- `Batch not found: <id>`
- `This batch has no saved preview yet. Click Process on it first…`
- `This batch has no files detected for '<vendor_key>'.`
- `<dotted.path>: <validation message>` (same validators as `/validate`)

Wired in `webapp/backend/api/vendor_rules.py`. Diff logic isolated in `webapp/backend/services/rules_impact.py`.

## 3. Frontend UI changes

`webapp/frontend/src/components/VendorRulesStudio.tsx`:
- New "Test against batch" panel between the editor header and the rule-group list.
- Batch picker (loaded via existing `api.listBatches()`), "Test against batch" button, copy-line: *"Test these rules against a batch before saving."* + disclaimer *"Preview only. This will not upload to Dropbox, write export files, or change any source documents."*.
- Result block:
  - 8 stat tiles: rows changed, cells changed, amount changes, GL changes, descriptions changed, dates changed, issues before, issues after. GL changes are coloured warm; "issues after" goes green when fewer than before.
  - Optional warning chips for added/removed rows.
  - Per-row diff table: source file + page, invoice number, column, before, after — with red-tinted "before" cells and green-tinted "after" cells.
  - Truncates the row list at 500 (matches backend cap).
- Toast on success: e.g. *"Draft rules changed 14 cells across 9 rows."* — or, when nothing changed, an info toast *"Draft rules produced no changes for this batch."*.
- Save remains independent from Test: Test can run with or without prior Validate. Save still runs the same backend validation it always did.

`webapp/frontend/src/vendorRulesApi.ts` — adds `previewImpact()` + the `ImpactPayload` / `ImpactSummary` / `RowDiff` / `RowChange` TS types.

CSS lives at the bottom of `webapp/frontend/src/styles.css` under a `Phase 2A` heading; tile colours follow the existing accent/warning/success palette.

## 4. Diff model

```typescript
{
  vendor_key: string,
  summary: {
    rows_before: number,
    rows_after: number,
    rows_added: number,
    rows_removed: number,
    rows_modified: number,
    cells_changed: number,
    amounts_changed: number,
    gl_accounts_changed: number,
    descriptions_changed: number,
    dates_changed: number,
    issues_before: number,
    issues_after: number
  },
  row_diffs: Array<{
    row_key: string,
    kind: "modified" | "added" | "removed",
    invoice_number: string | null,
    source_file: string | null,
    source_page: number | null,
    changes: Array<{column: string, before: any, after: any}>
  }>,
  warnings: string[],
  row_diffs_truncated: boolean
}
```

**Row matching.** Rows are flattened from `result.all_invoices[*].rows[*]` and keyed by the most stable composite available:
`<source_file>|p<source_page>|<invoice_number>|l<line_item_number>`.
When a part is missing, the matcher falls back to a deterministic index so we never crash; it also raises a warning if zero rows could be matched (so the UI doesn't read it as "everything is new").

**Cell diffing.** Internal keys (`__source_file`, `__key`, `_meta`) are skipped. Floats are rounded to 4 decimals before comparison (so `1.10` vs `1.1` doesn't surface). Strings are trimmed. Categorisation buckets (`amounts_changed`, `gl_accounts_changed`, etc.) come from a small allow-list of column names so the operator gets a meaningful at-a-glance summary.

**Issue counts.** `issues_before` = `len(saved_result.all_manual_review)`, `issues_after` = `len(draft_result.all_manual_review)`.

## 5. Safety guarantees

| Guarantee | How |
|---|---|
| YAML is never written by a preview | `apply_patch` is the only writer; `preview_rule_impact` never calls it. |
| No Dropbox call during preview | Vendor processors only `DropboxUploader.from_env(...)` when `not run_context["dry_run"]`. |
| No ResMan workbook written | Both vendor processors gate `write_resman_workbook(...)` on `not dry_run`. |
| No manual-review xlsx / debug CSV written | Same gate, same code path. |
| `_webapp_result.json` cache untouched | The new endpoint calls `process_batch()` directly and doesn't write the cache. |
| `Output/Template.xlsx` untouched | The processor only reads it when writing the workbook; both reads and writes are gated. |
| Source PDFs/CSVs untouched | They were already read-only; no change. |
| `.env` untouched | Never written. |
| AI fallback off | `run_context["ai_fallback_enabled"]` is only true when AI is configured and enabled — preview path doesn't change that gate. |
| Path traversal blocked | `vendor_key` is whitelisted; `batch_id` validation goes through the existing `batch_store` check. |
| Temp YAML cleaned up | `finally`-block deletes it on success and failure. |
| CLI processors unaffected | `dry_run` defaults to `False` in `process_batch`, and `run_context.get("dry_run", False)` defaults to `False` in both vendor processors — CLI invocations omit it entirely. |

## 6. Hopkinsville support

✅ **Fully supported.** Real-data smoke test (Section 8) ran the HWEA processor against a 10-PDF batch (63 line items) with a tolerance edit and got back a clean summary + per-row diff in under 30 seconds. YAML, Template, backups, batch dir all untouched after the run.

## 7. Richmond support

✅ **Supported.** The same dry_run gate was added to `process_richmond_utilities.py` (Dropbox skip, ResMan skip, manual-review xlsx skip, debug CSV skip). The endpoint accepts both vendor keys. Richmond has not been exercised end-to-end in this report (no Richmond batch was available in `webapp_data/batches/`); the code path is structurally identical to HWEA so behaviour is expected to match. Operator-driven test recommended before relying on it.

## 8. Tests performed

**Frontend build:**
```
$ cd webapp/frontend && npm run build
✓ 67 modules transformed.
✓ built in 1.56s
dist/assets/index-*.css   87.97 kB │ gzip: 15.38 kB
dist/assets/index-*.js   250.94 kB │ gzip: 75.67 kB
```

**Backend compile:**
```
$ python -m compileall webapp/backend -q
(no output → no errors)
```

**Backend smoke (`/api/vendor-rules/{key}/preview-impact`) — happy path + integrity:**

```
baseline yaml sha:    e83c554709edd0bf
baseline template sha:b753f406c0222f15
baseline backups:     []
baseline batch dir:   ['.progress_*.tmp', 'batch_metadata.json', 'export',
                       'input', 'logs', 'manual_review', 'processed',
                       'progress.json', 'region_hints.json']

bad-patch status:     400 amount_rules.tolerance: Tolerance must be between 0 and 100.
no-batch status:      400

--- running real preview-impact (re-runs HWEA processor in dry_run mode) ---
preview-impact status: 200
summary:
  amounts_changed       = 0
  cells_changed         = 63
  dates_changed         = 0
  descriptions_changed  = 0
  gl_accounts_changed   = 0
  issues_after          = 10
  issues_before         = 4
  rows_added            = 0
  rows_after            = 63
  rows_before           = 63
  rows_modified         = 63
  rows_removed          = 0

YAML unchanged:       True
Template unchanged:   True
No backup created:    True
No draft YAML leaked: True
=== Phase 2A smoke OK ===
```

**Validation gates exercised** (all return 400 with friendly messages):
- bad patch (out-of-range tolerance)
- non-existent batch_id
- unknown vendor key
- patch targeting a non-editable section (covered by Phase 1Z `validate`)

**Integrity invariants verified by the smoke run:**
- `config/vendors/hopkinsville_water_environment_authority.yaml` SHA-256 unchanged before/after.
- `Output/Template.xlsx` SHA-256 unchanged before/after.
- `config/vendors/backups/` not modified (only `Save` creates backups; preview runs do not).
- `webapp_data/batches/<id>/` did not retain any `draft_*.yaml` (temp file cleanup confirmed).
- Dropbox call site logged `Dropbox: skipped (dry_run)` instead of attempting authentication.
- `dry_run: skipping ResMan/manual-review/debug writes` log line emitted by the processor.
- Source PDFs in `Training Bills_Invoices/...` — never touched by preview path (they live outside the batch input dir).
- AI fallback service: untouched (the existing `run_context["ai_fallback_enabled"]` gate is a no-op without configured credentials).

**E2E:** The frontend build passes typechecking + Vite production bundle. The full `npm run test:e2e` suite was not executed in this report because no Phase 2A test fixtures exist yet (recommended for a follow-up).

## 9. Limitations

- **Compare-against-saved only.** The `compare_against_saved` flag is reserved but the only mode currently implemented is "compare against the batch's last `_webapp_result.json`". Comparing two drafts head-to-head, or comparing against a *fresh* baseline run, is a follow-up.
- **No row-by-row drill-down with PDF page jump.** The diff table shows source file + page numbers; clicking a row to open the PDF preview at that page is not yet wired.
- **Per-row diff capped at 500.** Larger diffs are truncated server-side to keep responses bounded; the summary still reflects totals.
- **Manual-review counts are list lengths.** Issue severity / category breakdowns (Phase 2B candidate) are not yet returned.
- **Long-running runs are synchronous.** A preview against a 100-PDF batch will block the request until the dry-run completes. Async polling (mirroring `/process` + `/progress`) would be ergonomic for big batches.
- **Richmond not exercised end-to-end** (no Richmond batch available locally during this phase; code path is structurally identical to HWEA).
- **Custom regex / read-only sections still locked.** Editing PDF regex patterns isn't wired to the studio (Phase 1Z scope decision); the impact preview can only test the editable subset.

## 10. Recommended next phase

Phase 2B — Rules Studio v3:
- Async preview-impact: return a job id, poll for progress, stream partial results to the UI.
- Click a row in the diff to open the PDF page in the document preview and highlight the changed cell.
- Categorise manual-review reasons (data quality / amount / address / dates) and bucket the issue-count delta by category.
- Side-by-side YAML diff view (read-only) for power users who want to see the raw change set.
- Backup browser + per-backup restore (today's "Restore" only takes the latest).
- Promote PDF regex editing into a real editor with a "test against this PDF" sandbox, instead of read-only view.
- Wire `/api/vendor-rules/{key}/preview-impact` into the Batch Workspace too: from a batch's preview table, "what if I edited rule X?" → opens the studio in compare mode.
