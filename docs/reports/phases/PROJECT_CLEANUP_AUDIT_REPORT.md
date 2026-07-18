# Project Cleanup Audit Report — Phase PERF-1

> **Companion to**: `WEBAPP_PHASE_PERF1_FULL_SYSTEM_PERFORMANCE_AUDIT_AND_OPTIMIZATION_REPORT.md`
> **Run on**: 2026-05-14
> **Scope**: identify clutter, document safe-cleanup candidates, harden `.gitignore`. **No files were moved or deleted by this audit** — every move/delete is explicitly recommended below for the operator to decide.

## TL;DR

| Concern | Bytes | Action taken | Recommendation |
|---|---:|---|---|
| Root scratch from initial bootstrap | ~168 KB | none — already gitignored | **Move to `docs/archive/bootstrap-may-1/`** to declutter repo root |
| Old PowerShell bootstrap scripts | ~76 KB | none — already gitignored | **Move to `docs/archive/bootstrap-may-1/`** |
| `webapp/_phase1d_*` temp artefacts | ~13 KB | none — already gitignored | **Delete** — already documented in WEBAPP_PHASE_1D report |
| Phase report screenshots (`docs/reports/phases/screenshots/`) | varies | none — keep as deliverables | Keep |
| OCR cache `webapp_data/cache/ocr/` | runtime only | added explicit `.gitignore` entry | Already covered by `webapp_data/` parent ignore — entry is documentation |

## 1 — Scratch files in repo root

These were generated during the initial categorisation work (May 1) and are no longer referenced by any script. They are already `.gitignore`'d so they don't affect the repo; the only concern is they crowd `ls` on the project root.

| File | Size | First seen | Referenced by code? |
|---|---:|---|---|
| `_vendor_mapping.csv` | 92 KB | May 1 16:51 | No (consumed by `_categorize_and_create_folders.ps1` only) |
| `_full_mapping_rows.txt` | 48 KB | May 1 16:52 | No |
| `_unique_vendors.txt` | 24 KB | May 1 16:32 | No |
| `_cat_summary_rows.txt` | 4 KB | May 1 16:52 | No |
| `_build_gl_analysis.ps1` | 12 KB | May 1 17:40 | No |
| `_build_gl_reference_yaml.ps1` | 12 KB | May 1 17:31 | No |
| `_build_vendor_yamls.ps1` | 32 KB | May 1 17:37 | No (its outputs live under `config/vendors/`) |
| `_categorize_and_create_folders.ps1` | 20 KB | May 1 16:51 | No (its outputs live under `Training Bills_Invoices/`) |

**Recommendation**: create `docs/archive/bootstrap-may-1/` and move all of the above into it. Add a `README.md` inside that folder explaining what each script did. This preserves provenance without cluttering the repo root. **Not done automatically** — the operator should approve the move.

## 2 — `webapp/_phase1d_*`

These are byproducts of a Phase 1D regression test (the export workbook + a batch fixture). They are referenced by `WEBAPP_PHASE_1D_LAYOUT_AND_PDF_LINKS_REPORT.md` as evidence but are not consumed by any current code.

**Recommendation**: safe to delete after confirming the Phase 1D report is finalised. Already `.gitignore`'d so they don't appear in commits today.

## 3 — Phase report screenshots (`docs/reports/phases/screenshots/`)

Inventoried directories:
- `bulk_single_ai_cell_polish/`
- `phase_1s_after/`, `phase_1s_before/`
- `phase_1t_responsive/`
- `phase_1w_batch_file_manager/{before,after}/`
- (plus new `phase_perf1/` added by this phase)

**Recommendation**: keep — these are report deliverables. Total size is small (< 30 MB) and they cross-reference markdown reports.

## 4 — Generated runtime data

| Path | Status | Notes |
|---|---|---|
| `webapp_data/batches/` | Covered by `webapp_data/` in `.gitignore` | Per-batch input/processed/audit files. Audit dir now includes `performance.json` (added by Phase PERF-1). |
| `webapp_data/cache/ocr/` | Now explicitly listed in `.gitignore` | New in Phase PERF-1. Safe to delete to force re-OCR of cached files. |
| `**/Processed_Output/` | Already in `.gitignore` | Per-vendor processor scratch. |
| `Output/Template.xlsx` | **Source of truth — never modify** | Phase PERF-1 verified untouched. |

## 5 — Reports folder

`docs/reports/phases/` holds **58 markdown files** covering every phase of the webapp. The folder is healthy and well-organised. No moves recommended.

## 6 — `.gitignore` changes applied by Phase PERF-1

Added explicit documentation entry for the new OCR cache location:

```gitignore
# Phase PERF-1 — runtime artefacts.
webapp_data/cache/ocr/
```

The cache is also covered by the existing `webapp_data/` blanket rule. The explicit entry exists so future contributors immediately see where the cache lives and how to invalidate it (delete the directory).

## 7 — Items explicitly NOT touched

Per the Phase PERF-1 constraints:

| Path | Reason |
|---|---|
| `Output/Template.xlsx` | ResMan source of truth |
| `Training Bills_Invoices/` | Source training data |
| `Old Scripts/` | Historical reference for vendor migration |
| `.env` | Contains live credentials |
| Any vendor processor `.py` source | Deterministic logic must not regress |

## 8 — Recommended follow-up (separate cleanup PR)

If the operator approves a follow-up cleanup pass:

```bash
mkdir -p docs/archive/bootstrap-may-1
git mv _vendor_mapping.csv docs/archive/bootstrap-may-1/
git mv _full_mapping_rows.txt docs/archive/bootstrap-may-1/
git mv _unique_vendors.txt docs/archive/bootstrap-may-1/
git mv _cat_summary_rows.txt docs/archive/bootstrap-may-1/
git mv _build_*.ps1 docs/archive/bootstrap-may-1/
git mv _categorize_and_create_folders.ps1 docs/archive/bootstrap-may-1/
git rm webapp/_phase1d_batch.txt webapp/_phase1d_export.xlsx
```

Followed by adding `docs/archive/bootstrap-may-1/README.md` describing each file's original purpose. The moves are tracked by git so history is preserved.

---

# Phase PERF-2 Addendum

> **Run on**: 2026-05-21<br>
> **Scope**: performance-related cleanup audit only. No source bills, `.env`,
> `Output/Template.xlsx`, or `Old Scripts/` were modified.

## Safe Moves Performed

No repository files were moved or deleted. Runtime-only temporary profiling
batches created by `scripts/profile_processing_performance.py` were removed
from `webapp_data/batches/` after verifying each path resolved under the
runtime batches directory.

## Files Left Intentionally

| Path | Decision |
|---|---|
| `docs/reports/phases/screenshots/phase_perf2/` | Kept as PERF-2 evidence: baseline/current metrics, profile JSON, and QA screenshots. |
| `tmp_3046895_p1.png`, `tmp_3046895_p2.png`, `tmp_3046912_p1.png`, `tmp_3046912_p2.png` | Left untouched because they pre-existed this phase and may belong to visual debugging. |
| `webapp/frontend/src/brand-refresh.css` | Left untouched; visual refresh work predates PERF-2. |
| `webapp_data/` | Runtime data; still ignored by `.gitignore`. |

## Cleanup Opportunities Deferred

| Opportunity | Reason deferred |
|---|---|
| Splitting `webapp/frontend/src/styles.css` | The file is large, but PERF-2 prioritized runtime fluidity. CSS module splitting would be high visual-regression risk in this phase. |
| Removing old phase screenshots | They are report deliverables and should remain available for visual regression comparison. |
| Deleting old root scratch PNGs | They are untracked/user-adjacent artifacts; not removed without explicit approval. |
| Pruning stale runtime batches | Not safe to infer which batches are operator data. Only profiler-owned temp batches were removed. |

## Ignore Coverage Verified

`.gitignore` already covers `.env`, `webapp_data/`, frontend build output,
`node_modules/`, `Output/`, `Training Bills_Invoices/`, and `Old Scripts/`.
No additional ignore rule was required for PERF-2.
