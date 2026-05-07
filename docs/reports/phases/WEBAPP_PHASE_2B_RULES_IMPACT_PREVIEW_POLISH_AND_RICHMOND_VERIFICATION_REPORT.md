# Phase 2B — Rules Studio impact preview polish & Richmond verification

**Date:** 2026-05-03
**Scope:** Stop counting Dropbox-skip artefacts as rule changes in the Vendor Rules Studio "Test against batch" output. Verify the fix end-to-end against the user-reported Richmond Utilities scenario.

---

## 1. Problem reported

Running "Test against batch" on Richmond returned `cells_changed: 16`, with **every** change in the `Document Url` column. That's misleading: dry-run skips Dropbox uploads (`run_context["dry_run"]` gates `DropboxUploader.from_env(...)` in `process_richmond_utilities.py`), so the saved baseline carries real Dropbox URLs and the dry-run draft carries blanks. The diff is technically correct but operationally worthless — the user sees noise instead of *"did my rule edit do anything?"*.

## 2. Fix — backend (`webapp/backend/services/rules_impact.py`)

A new classifier separates **meaningful** rule-driven changes from **dry-run technical artefacts**:

```python
_DRY_RUN_LINK_COLUMNS = {
    "Document Url", "Document URL",
    "Support Document Url", "Support Document URL",
    "Dropbox Url", "Dropbox URL",
    "Attachment Url", "Attachment URL",
}

def _is_dry_run_link_change(change):
    if change["column"] not in _DRY_RUN_LINK_COLUMNS:
        return False
    return _looks_blank_link(change["before"]) != _looks_blank_link(change["after"])
```

`_looks_blank_link()` treats `None`, `""`, `"-"`, `"—"`, `"n/a"`, `"none"` as blank. Only the **blank ↔ non-blank flip** is muted — a real-URL → different-real-URL change still counts (rare in practice, but the design preserves it for safety).

Each row change carries a `category` field:
- `"meaningful"` — counted in `cells_changed`, `amounts_changed`, `gl_accounts_changed`, `descriptions_changed`, `dates_changed`.
- `"dry_run_link"` — counted only in `dry_run_only_link_changes`. Excluded from the primary metrics.

The summary now exposes:

| Field | Semantics |
|---|---|
| `cells_changed` | meaningful subset (Phase 2A's value, minus dry-run flips) |
| `cells_changed_total` | every cell change including link flips (kept for context) |
| `dry_run_only_link_changes` | new — count of muted link flips |
| `rows_modified` | rows with ≥ 1 meaningful change |
| `rows_modified_dry_run_only` | rows whose only changes are link flips |
| `amounts_changed`, `gl_accounts_changed`, `descriptions_changed`, `dates_changed` | unchanged semantics, but now never tick from a link flip |
| `issues_before`, `issues_after` | unchanged |

The response carries two new top-level fields:

```json
{
  "no_meaningful_impact": true,
  "no_meaningful_impact_message":
    "No meaningful rule impact detected. Support document links differ because dry-run skips Dropbox."
}
```

`no_meaningful_impact` is `true` iff `cells_changed == 0`, no rows were added or removed, and `dry_run_only_link_changes > 0`.

Each `RowDiff` also gains `has_meaningful_changes` and `has_dry_run_link_changes` booleans so the UI can hide noise rows without re-walking the change list.

## 3. Fix — frontend (`webapp/frontend/src/components/VendorRulesStudio.tsx`)

**Summary tiles** rebuilt around the meaningful/technical split:

1. Meaningful cells changed
2. Amount changes
3. GL changes (warm tone if > 0)
4. Description changes
5. Date changes
6. Issues before
7. Issues after (green tone if dropped)
8. Dry-run-only link changes (muted tone — visually distinct from real impact)

**Empty-impact banner** — when `no_meaningful_impact === true`, a banner renders the backend's exact message above the tiles so the operator immediately reads *"the test ran, the rule edit didn't move the needle"*.

**Toast wording** — when only dry-run link flips remain:
*"No meaningful rule impact. Only support-document links differ (dry-run skips Dropbox)."*

**Diff table** — the default view filters out:
- rows whose only changes are dry-run link flips, AND
- per-row `dry_run_link` changes on rows that *do* have meaningful changes (so the table reads as pure rule effect).

A new `Show dry-run technical differences (N link change(s))` toggle exposes the muted rows on demand. When shown, link rows render in italic muted text with a small `dry-run` tag next to the column name, so they're visually distinct from meaningful rows.

**Updated TypeScript types** (`vendorRulesApi.ts`) — `ImpactSummary`, `RowChange.category`, `RowDiff.has_meaningful_changes`, `RowDiff.has_dry_run_link_changes`, `ImpactPayload.no_meaningful_impact{,_message}`.

## 4. Columns excluded from "cells changed" by default

`Document Url`, `Document URL`, `Support Document Url`, `Support Document URL`, `Dropbox Url`, `Dropbox URL`, `Attachment Url`, `Attachment URL`. All 8 listed in the spec are recognised; case differences match exactly because vendor processors emit different casings.

## 5. Columns NEVER muted — real business changes

`Amount`, `Tax`, `Unit Price`, `Quantity`, `GL Account`, `Invoice Description`, `Line Item Description`, `Service Address`, `Invoice Date`, `Accounting Date`, `Due Date`, `Payment Date`, `Property Abbreviation`, `Location`, `Expense Type` — and any other column the dry-run gate does not touch — flow through unchanged. Manual review issues are tracked separately via `issues_before`/`issues_after`.

## 6. Richmond verification — user's reported scenario

Re-ran Richmond preview-impact against the user's exact PDF (`Richmond Utilities - Blue Country 4-6-26.pdf`, 14 pages, 16 line items). To reproduce a production-like baseline (Dropbox configured), the smoke seeded the cached `_webapp_result.json` with 16 fake `Document Url` values.

**Backend response with empty patch (no rule edits) — Phase 2B output:**

```
cells_changed             (meaningful) = 0
dry_run_only_link_changes              = 16
cells_changed_total                    = 16
rows_modified             (meaningful) = 0
rows_modified_dry_run_only             = 16
amounts_changed                        = 0
gl_accounts_changed                    = 0
descriptions_changed                   = 0
dates_changed                          = 0
issues_before                          = 14
issues_after                           = 14
no_meaningful_impact                   = True
message  = "No meaningful rule impact detected.
            Support document links differ because dry-run skips Dropbox."
```

Every change in `row_diffs` was tagged `category: "dry_run_link"`. **All eight expected values from the spec match exactly.**

## 7. Counter-test — meaningful changes still reported

With the same Richmond batch, 16 fake URL flips PLUS 2 fake Amount drifts were injected:

```
cells_changed             (meaningful) = 2
amounts_changed                        = 2
dry_run_only_link_changes              = 16
no_meaningful_impact                   = False
```

Confirms the filter doesn't swallow real rule effects when they coexist with dry-run noise.

## 8. Tests performed

| Test | Result |
|---|---|
| `npm run build` (frontend) | ✓ 67 modules, 252.04 kB JS / 89.00 kB CSS |
| `python -m compileall webapp/backend` | ✓ no errors |
| Richmond preview-impact, empty patch, baseline with 16 fake Dropbox URLs | meaningful=0, dry-run=16, message correct |
| Richmond preview-impact, mixed diff (16 URLs + 2 Amount drifts) | meaningful=2, amounts_changed=2, dry-run=16, no-impact=False |
| Richmond preview-impact, real-data batch with Dropbox unconfigured | 0 / 0 (no diff at all — both sides blank) |

**Integrity invariants** verified during the Richmond smoke:
- `config/vendors/richmond_utilities.yaml` SHA-256 unchanged.
- `Output/Template.xlsx` SHA-256 unchanged.
- Source PDF unchanged.
- No new `config/vendors/backups/*.yaml`.
- No `draft_*.yaml` left in the batch directory.
- Dropbox: log line `Dropbox: skipped (dry_run)` confirmed.
- ResMan/manual-review/debug writes: log line `dry_run: skipping ResMan/manual-review/debug writes` confirmed.

## 9. Files touched

- `webapp/backend/services/rules_impact.py` — diff classifier + summary fields + no-impact flag.
- `webapp/frontend/src/vendorRulesApi.ts` — TS types updated.
- `webapp/frontend/src/components/VendorRulesStudio.tsx` — tiles, toggle, banner, table filter, toast wording.
- `webapp/frontend/src/styles.css` — `.rules-impact-stat.tone-muted`, `.rules-impact-no-impact`, `.rules-impact-toggle`, `.rules-impact-tag`, `.rules-impact-table tr.is-dry-run-link`.

## 10. Limitations / follow-ups

- The classifier assumes the canonical column names listed in §4. A future vendor that emits a differently-named link column would slip through. If we add such a vendor we should drive the list from `support_document_rules.column_name` in the YAML rather than a Python set.
- Real-URL → different-real-URL changes still count as meaningful. That's deliberate (it's the behavior the user would want for *"what if I switch to a new bucket?"*), but worth flagging.
- The toggle to reveal technical differences hides the row IDs of dry-run-only rows by default — a power-user view that lists them separately could be a Phase 2C nicety.
- HWEA already produced 0 meaningful Phase 2B changes for the tolerance edit because tolerance only flips manual-review counts, not row cells. The `issues_before` vs `issues_after` tiles already surface that delta correctly; no change needed.
