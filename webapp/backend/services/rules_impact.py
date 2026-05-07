"""Phase 2A — Vendor Rules Studio "Test against batch" impact preview.

Glue layer that:

1. Takes a vendor's *current saved rules* + a *draft patch* the operator
   is auditioning in the studio.
2. Materialises the draft rules into a temp YAML file.
3. Re-runs ``batch_processor.process_batch`` against an existing batch
   with ``dry_run=True`` so nothing on disk changes (no Dropbox upload,
   no ResMan workbook write, no debug CSV, no manual-review xlsx, no
   webapp result-cache write).
4. Diffs the resulting rows against the batch's saved preview cache.
5. Returns a UI-shaped diff summary + per-row changes.

The actual processor call is the same one the webapp already uses; the
only Phase 2A additions are:

  * A ``dry_run`` flag in ``run_context`` (vendor processors gate Excel /
    Dropbox writes off this).
  * A ``rules_override_paths`` argument on ``batch_processor.process_batch``
    so we can swap in the draft YAML *only for the targeted vendor*.

CLI runs are unchanged: the CLI never sets ``dry_run`` or supplies
override paths, so its behaviour is byte-for-byte identical.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

import yaml

from . import batch_store, batch_processor
from .vendor_rules import (
    EDITABLE_PREFIXES,
    VendorRulesError,
    _check_vendor_key,
    _flatten,
    _set_dotted,
    load_vendor_rules,
    validate_patch,
)


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------


def preview_rule_impact(
    vendor_key: str,
    batch_id: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Run a dry-run reprocess with the draft rules and return a diff vs. the
    batch's last saved preview.

    Raises ``VendorRulesError`` for caller-visible problems (bad vendor key,
    bad batch, schema-invalid patch, or vendors not yet supported by the
    dry-run plumbing).
    """
    _check_vendor_key(vendor_key)

    # 1. Schema-level validation of the patch (same code the editor calls).
    issues = validate_patch(vendor_key, patch or {}) if patch else []
    if issues:
        raise VendorRulesError(_format_issues(issues))

    # 2. Locate the batch + its existing preview cache (the baseline).
    try:
        bdir = batch_store.get_batch_dir(batch_id)
    except FileNotFoundError as e:
        raise VendorRulesError(f"Batch not found: {batch_id}") from e

    saved_cache = bdir / "processed" / "_webapp_result.json"
    if not saved_cache.is_file():
        raise VendorRulesError(
            "This batch has no saved preview yet. Click Process on it first, "
            "then come back to test draft rules.",
        )
    saved_result = _load_json(saved_cache)

    # 3. Verify the batch actually contains files for this vendor (otherwise
    #    the dry-run will return zero rows and the user gets a confusing
    #    "no changes" result).
    if not _batch_has_vendor_files(saved_result, vendor_key):
        raise VendorRulesError(
            "This batch has no files detected for "
            f"'{vendor_key}'. Pick a batch that contains "
            f"{_pretty_vendor(vendor_key)} bills."
        )

    # 4. Build the draft rules dict by overlaying the patch on the current
    #    saved YAML.
    saved_rules = load_vendor_rules(vendor_key)
    draft_rules = copy.deepcopy(saved_rules)
    for dotted, value in _flatten(patch or {}).items():
        if not _is_editable(dotted):
            # Schema validation already caught this; defensive only.
            raise VendorRulesError(
                f"Field '{dotted}' is not editable in the studio."
            )
        _set_dotted(draft_rules, dotted, value)

    # 5. Materialise the draft to a temp YAML and run the dry-run pass.
    fd, tmp_str = tempfile.mkstemp(
        prefix=f"draft_{vendor_key}_",
        suffix=".yaml",
        dir=str(bdir),
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            yaml.safe_dump(
                draft_rules,
                f,
                sort_keys=False,
                default_flow_style=False,
                allow_unicode=True,
                width=100,
            )
        try:
            draft_result = batch_processor.process_batch(
                batch_id,
                dry_run=True,
                rules_override_paths={vendor_key: tmp_path},
            )
        except Exception as e:  # noqa: BLE001 — surface to caller as friendly error
            raise VendorRulesError(
                "Could not run impact preview against this batch: "
                f"{type(e).__name__}: {e}",
            ) from e
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    # 6. Diff. Returns the UI-shaped payload directly.
    diff = _compute_diff(
        saved_result=saved_result,
        draft_result=draft_result,
        vendor_key=vendor_key,
    )
    return diff


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _is_editable(dotted: str) -> bool:
    return any(dotted == p or dotted.startswith(p + ".") for p in EDITABLE_PREFIXES)


def _format_issues(issues: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    for it in issues:
        p = it.get("path") or "(root)"
        m = it.get("message") or "Invalid value."
        parts.append(f"{p}: {m}")
    return " | ".join(parts)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise VendorRulesError(
            f"Could not read saved preview cache: {type(e).__name__}",
        ) from e


def _batch_has_vendor_files(result: dict[str, Any], vendor_key: str) -> bool:
    detection = result.get("detection") or {}
    if isinstance(detection, dict):
        for v in detection.values():
            if isinstance(v, str) and v == vendor_key:
                return True
            if isinstance(v, dict) and v.get("vendor_key") == vendor_key:
                return True
    by_vendor = result.get("by_vendor") or {}
    if vendor_key in by_vendor:
        return True
    return False


def _pretty_vendor(vendor_key: str) -> str:
    return vendor_key.replace("_", " ").title()


# ----------------------------------------------------------------------------
# Diff computation
# ----------------------------------------------------------------------------

# Columns the UI should report at finer granularity in the summary.
_AMOUNT_COLUMNS = {"Amount", "Tax", "Unit Price", "Quantity"}
_GL_COLUMNS = {"GL Account"}
_DATE_COLUMNS = {"Invoice Date", "Accounting Date", "Due Date", "Payment Date"}
_DESCRIPTION_COLUMNS = {
    "Invoice Description",
    "Line Item Description",
    "Service Address",
}

# Phase 2B — Columns whose value flips purely because dry-run skips
# Dropbox uploads. These are NOT business rule changes — when the bar is
# in dry-run mode the support-document URL goes from a real Dropbox link
# (saved baseline) to blank/dash (draft run), and that flip would
# otherwise dominate every diff for vendors that upload supports.
#
# We flag them in a separate metric (``dry_run_only_link_changes``) so
# the operator can see they happened without them polluting the
# "meaningful cells changed" count.
_DRY_RUN_LINK_COLUMNS = {
    "Document Url",
    "Document URL",
    "Support Document Url",
    "Support Document URL",
    "Dropbox Url",
    "Dropbox URL",
    "Attachment Url",
    "Attachment URL",
}


def _is_dry_run_link_change(change: dict[str, Any]) -> bool:
    """Detect a change that's almost certainly *just* the Dropbox skip.

    Two conditions:
      * the column matches a known support-link column, AND
      * exactly one side is non-empty while the other is empty / dashed
        (the dry-run path can't produce a Dropbox URL, so it shows blank
        or '—' against a real URL on the saved side).

    A real-URL → different-real-URL change would still count as a
    meaningful change; we only mute the no-link / yes-link flip.
    """
    col = change.get("column")
    if col not in _DRY_RUN_LINK_COLUMNS:
        return False
    before = change.get("before")
    after = change.get("after")
    return _looks_blank_link(before) != _looks_blank_link(after)


def _looks_blank_link(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip().lower()
        return s in ("", "-", "—", "n/a", "none")
    return False


def _compute_diff(
    *,
    saved_result: dict[str, Any],
    draft_result: dict[str, Any],
    vendor_key: str,
) -> dict[str, Any]:
    saved_rows = _flatten_rows(saved_result, vendor_key)
    draft_rows = _flatten_rows(draft_result, vendor_key)

    saved_by_key = {r["__key"]: r for r in saved_rows}
    draft_by_key = {r["__key"]: r for r in draft_rows}

    common_keys = [k for k in saved_by_key.keys() if k in draft_by_key]
    only_saved = [k for k in saved_by_key.keys() if k not in draft_by_key]
    only_draft = [k for k in draft_by_key.keys() if k not in saved_by_key]

    cells_changed = 0  # meaningful cell changes only (excludes dry-run link flips)
    cells_changed_total = 0  # everything, including dry-run-only flips
    dry_run_only_link_changes = 0
    amounts_changed = 0
    gl_accounts_changed = 0
    descriptions_changed = 0
    dates_changed = 0
    rows_modified_meaningful = 0
    rows_dry_run_only = 0  # rows whose only differences are dry-run link flips
    row_diffs: list[dict[str, Any]] = []

    for key in common_keys:
        before = saved_by_key[key]
        after = draft_by_key[key]
        changes = _row_changes(before, after)
        if not changes:
            continue

        meaningful_changes: list[dict[str, Any]] = []
        link_changes: list[dict[str, Any]] = []
        for ch in changes:
            cells_changed_total += 1
            if _is_dry_run_link_change(ch):
                # Tag the change so the UI can render it under a
                # collapsed "dry-run technical differences" section.
                ch = {**ch, "category": "dry_run_link"}
                dry_run_only_link_changes += 1
                link_changes.append(ch)
                continue
            ch = {**ch, "category": "meaningful"}
            meaningful_changes.append(ch)
            cells_changed += 1
            col = ch["column"]
            if col in _AMOUNT_COLUMNS:
                amounts_changed += 1
            if col in _GL_COLUMNS:
                gl_accounts_changed += 1
            if col in _DESCRIPTION_COLUMNS:
                descriptions_changed += 1
            if col in _DATE_COLUMNS:
                dates_changed += 1

        if meaningful_changes:
            rows_modified_meaningful += 1
        elif link_changes:
            rows_dry_run_only += 1

        row_diffs.append(
            {
                "row_key": key,
                # ``modified`` rows still show as modified in the table; the
                # frontend uses ``has_meaningful_changes`` to decide whether
                # to surface it in the default view or collapse it under the
                # "Show dry-run technical differences" toggle.
                "kind": "modified",
                "has_meaningful_changes": bool(meaningful_changes),
                "has_dry_run_link_changes": bool(link_changes),
                "invoice_number": after.get("Invoice Number")
                or before.get("Invoice Number"),
                "source_file": after.get("__source_file")
                or before.get("__source_file"),
                "source_page": after.get("__source_page")
                or before.get("__source_page"),
                "changes": meaningful_changes + link_changes,
            }
        )

    for key in only_saved:
        before = saved_by_key[key]
        row_diffs.append(
            {
                "row_key": key,
                "kind": "removed",
                "invoice_number": before.get("Invoice Number"),
                "source_file": before.get("__source_file"),
                "source_page": before.get("__source_page"),
                "changes": [],
            }
        )
    for key in only_draft:
        after = draft_by_key[key]
        row_diffs.append(
            {
                "row_key": key,
                "kind": "added",
                "invoice_number": after.get("Invoice Number"),
                "source_file": after.get("__source_file"),
                "source_page": after.get("__source_page"),
                "changes": [],
            }
        )

    issues_before = _count_issues(saved_result)
    issues_after = _count_issues(draft_result)

    summary = {
        "rows_before": len(saved_rows),
        "rows_after": len(draft_rows),
        "rows_added": len(only_draft),
        "rows_removed": len(only_saved),
        # rows_modified now reflects only rows with at least one
        # *meaningful* change. Phase 2A counted every modification,
        # including dry-run-only link flips.
        "rows_modified": rows_modified_meaningful,
        "rows_modified_dry_run_only": rows_dry_run_only,
        # ``cells_changed`` is the meaningful subset; ``cells_changed_total``
        # is what the previous version returned (kept for completeness).
        "cells_changed": cells_changed,
        "cells_changed_total": cells_changed_total,
        "dry_run_only_link_changes": dry_run_only_link_changes,
        "amounts_changed": amounts_changed,
        "gl_accounts_changed": gl_accounts_changed,
        "descriptions_changed": descriptions_changed,
        "dates_changed": dates_changed,
        "issues_before": issues_before,
        "issues_after": issues_after,
    }

    warnings: list[str] = []
    if not common_keys and (saved_rows or draft_rows):
        # If we couldn't match any rows, the keys must have shifted in a
        # surprising way (e.g. invoice numbers regenerated). Flag for the
        # operator so the row-level diff isn't misread as "everything new".
        warnings.append(
            "No rows matched between the saved preview and the draft run; "
            "the diff is showing row additions/removals only."
        )

    # Phase 2B — the only differences are dry-run technical artefacts.
    # Surface a clear "no rule impact" message so operators don't
    # mistake the support-link flip for a real change.
    no_meaningful_impact = (
        cells_changed == 0
        and len(only_draft) == 0
        and len(only_saved) == 0
        and dry_run_only_link_changes > 0
    )

    # Cap the per-row diff list so the API response stays bounded. The UI
    # gets the summary regardless and can request a wider window later.
    MAX_ROWS = 500
    truncated = False
    if len(row_diffs) > MAX_ROWS:
        row_diffs = row_diffs[:MAX_ROWS]
        truncated = True

    return {
        "vendor_key": vendor_key,
        "summary": summary,
        "row_diffs": row_diffs,
        "warnings": warnings,
        "row_diffs_truncated": truncated,
        "no_meaningful_impact": no_meaningful_impact,
        "no_meaningful_impact_message": (
            "No meaningful rule impact detected. Support document links "
            "differ because dry-run skips Dropbox."
        ) if no_meaningful_impact else None,
    }


def _count_issues(result: dict[str, Any]) -> int:
    review = result.get("all_manual_review") or []
    if isinstance(review, list):
        return len(review)
    return 0


def _flatten_rows(result: dict[str, Any], vendor_key: str) -> list[dict[str, Any]]:
    """Flatten the per-invoice rows into a single list keyed for matching."""
    out: list[dict[str, Any]] = []
    invoices = result.get("all_invoices") or []
    for inv_index, inv in enumerate(invoices):
        if not isinstance(inv, dict):
            continue
        source_file = (
            inv.get("source_file")
            or (inv.get("debug_info") or {}).get("source_file")
            or ""
        )
        source_page = (
            inv.get("source_page")
            or (inv.get("debug_info") or {}).get("source_page")
            or 1
        )
        invoice_number = str(inv.get("invoice_number") or "").strip()
        rows = inv.get("rows") or []
        for line_index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            line_no = row.get("Line Item Number") or (line_index + 1)
            key_parts = [
                source_file or f"inv-{inv_index}",
                f"p{source_page}",
                invoice_number or f"i{inv_index}",
                f"l{line_no}",
            ]
            row = dict(row)
            row["__key"] = "|".join(str(p) for p in key_parts)
            row["__source_file"] = source_file or None
            row["__source_page"] = source_page or None
            row["__vendor_key"] = vendor_key
            out.append(row)
    return out


def _row_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the per-cell changes between two flattened rows.

    We ignore internal '__'-prefixed keys and the '_meta' bag (which can
    legitimately differ on every run for non-business fields like
    debugging counters).
    """
    changes: list[dict[str, Any]] = []
    keys = set(before.keys()) | set(after.keys())
    for k in sorted(keys):
        if k.startswith("__") or k == "_meta":
            continue
        b = before.get(k)
        a = after.get(k)
        if _normalize(b) == _normalize(a):
            continue
        changes.append({"column": k, "before": b, "after": a})
    return changes


def _normalize(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, float):
        # Round to cents so 1.10 vs 1.1 doesn't show as a "change".
        return round(v, 4)
    if isinstance(v, str):
        return v.strip()
    return v
