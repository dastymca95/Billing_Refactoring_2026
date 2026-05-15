"""Golden fixture library for canonical invoice reasoning.

Fixtures intentionally use cached extraction candidates instead of live AI
calls. They exercise the same validation/canonicalization/template-row path
used by AI-assisted invoices, while staying deterministic, cheap, and safe.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .. import settings
from . import ai_invoice_processor, canonical_rules


FIXTURE_ROOT = settings.PROJECT_ROOT / "webapp" / "backend" / "tests" / "fixtures" / "canonical_invoices"

CHECK_GROUPS = {
    "category": "Identity",
    "vendor": "Identity",
    "invoice_number": "Identity",
    "account_number": "Identity",
    "invoice_date": "Dates",
    "due_date": "Dates",
    "service_period": "Dates",
    "property_abbreviation": "Vendor / Property",
    "location": "Vendor / Property",
    "location_policy": "Vendor / Property",
    "gl_accounts": "GL",
    "line_items": "Amounts",
    "total": "Amounts",
    "merchandise": "Amounts",
    "tax": "Amounts",
    "invoice_description": "Descriptions",
    "review_flags": "Review flags",
    "ignored_source_items": "Review flags",
}


class CanonicalFixtureError(ValueError):
    """Raised when a fixture key or fixture file is invalid."""


def list_fixtures() -> dict[str, Any]:
    fixtures = []
    for fixture_dir in _fixture_dirs():
        source, expected = _load_fixture_files(fixture_dir.name)
        status = _fixture_status(source, expected)
        skip_reason = _skip_reason(source, expected) if status != "complete" else ""
        fixtures.append(
            {
                "key": fixture_dir.name,
                "vendor": source.get("vendor") or expected.get("vendor_expected") or fixture_dir.name,
                "category": source.get("category") or expected.get("category_expected") or "unknown",
                "description": source.get("description") or expected.get("notes") or "",
                "status": status,
                "requires_live_ai": bool(source.get("requires_live_ai")),
                "skip_reason": skip_reason,
                "last_result": _last_result_for_fixture(fixture_dir.name, status, skip_reason),
            }
        )
    return {"fixtures": fixtures}


def run_fixture(
    fixture_key: str,
    *,
    rules_override: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    source, expected = _load_fixture_files(fixture_key)
    status = _fixture_status(source, expected)
    if status != "complete":
        skip_reason = _skip_reason(source, expected)
        return {
            "ok": True,
            "skipped": True,
            "dry_run": dry_run,
            "fixture_key": fixture_key,
            "test_case": fixture_key,
            "title": source.get("description") or expected.get("notes") or fixture_key,
            "expected": expected,
            "actual": {},
            "checks": [],
            "extracted_candidates": source.get("extracted_candidates") or {},
            "canonical_application": {"status": status, "skip_reason": skip_reason},
            "rows": [],
            "review_flags": [],
            "skip_reason": skip_reason,
            "reasoning_timeline": [
                {
                    "step": "Fixture skipped",
                    "detail": skip_reason
                    or "This fixture is registered but its expected/candidate payload is not complete yet.",
                }
            ],
        }

    candidates = dict(source.get("extracted_candidates") or {})
    if not candidates:
        raise CanonicalFixtureError(f"Fixture '{fixture_key}' has no extracted_candidates payload.")
    candidates.setdefault("_source_file", source.get("source_document") or f"{fixture_key}.pdf")
    normalized = ai_invoice_processor.validate_ai_extraction(
        candidates,
        references=_fixture_references(),
        rules_override=rules_override,
    )
    invoice = ai_invoice_processor.ai_result_to_invoice(
        normalized,
        batch_id="batch_20990101_000000_000",
        source_file=str(source.get("source_document") or f"{fixture_key}.pdf"),
        vendor_key="canonical_fixture",
        support_document_url=str(source.get("support_document_url") or "https://dropbox.example/canonical/fixture.pdf"),
        support_document_status="uploaded",
        support_document_dropbox_path=f"/Billing/Canonical Fixtures/{fixture_key}",
    )
    rows = invoice.get("rows") or []
    actual = _actual_from_invoice(normalized, rows, source)
    checks = _checks_from_expected(expected, actual)
    return {
        "ok": all(check["pass"] for check in checks if check.get("required", True)),
        "skipped": False,
        "dry_run": dry_run,
        "fixture_key": fixture_key,
        "test_case": fixture_key,
        "title": source.get("description") or fixture_key,
        "expected": expected,
        "actual": actual,
        "checks": checks,
        "extracted_candidates": candidates,
        "canonical_application": {
            "category": normalized.get("category"),
            "category_label": normalized.get("category_label"),
            "canonical_rules_used": _rules_used(normalized),
            "blocking_required_fields": normalized.get("blocking_required_fields") or [],
            "manual_review_codes": normalized.get("manual_review_codes") or [],
            "validation_summary": normalized.get("validation_summary") or {},
            "total_reconciliation": {
                "passed": (normalized.get("validation_summary") or {}).get("total_reconciliation_passed"),
                "reconciled_total": (normalized.get("validation_summary") or {}).get("reconciled_total"),
                "invoice_total": (normalized.get("validation_summary") or {}).get("invoice_total"),
            },
        },
        "rows": rows,
        "review_flags": normalized.get("manual_review_issues") or [],
        "reasoning_timeline": _timeline(source, normalized, rows, actual),
    }


def run_all_complete(*, rules_override: dict[str, Any] | None = None) -> dict[str, Any]:
    results = []
    for fixture_dir in _fixture_dirs():
        result = run_fixture(fixture_dir.name, rules_override=rules_override)
        results.append(result)
    complete = [result for result in results if not result.get("skipped")]
    return {
        "ok": all(result.get("ok") for result in complete),
        "results": results,
        "summary": [
            {
                "fixture_key": result["fixture_key"],
                "status": "SKIPPED" if result.get("skipped") else ("PASS" if result.get("ok") else "FAIL"),
                "failed_checks": [
                    check["field"] for check in result.get("checks") or [] if not check.get("pass") and check.get("required", True)
                ],
                "skip_reason": result.get("skip_reason") or "",
            }
            for result in results
        ],
    }


def _fixture_dirs() -> list[Path]:
    if not FIXTURE_ROOT.is_dir():
        return []
    return sorted(
        [path for path in FIXTURE_ROOT.iterdir() if path.is_dir()],
        key=lambda path: path.name,
    )


@lru_cache(maxsize=1)
def _fixture_references() -> dict[str, list[dict[str, Any]]]:
    return ai_invoice_processor.load_references()


def _load_fixture_files(fixture_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    key = _safe_key(fixture_key)
    fixture_dir = FIXTURE_ROOT / key
    source_path = fixture_dir / "source_reference.json"
    expected_path = fixture_dir / "expected.yaml"
    if not source_path.is_file() or not expected_path.is_file():
        raise CanonicalFixtureError(f"Unknown canonical fixture '{fixture_key}'.")
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
        expected = yaml.safe_load(expected_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise CanonicalFixtureError(f"Could not load fixture '{fixture_key}': {exc}") from exc
    if not isinstance(source, dict) or not isinstance(expected, dict):
        raise CanonicalFixtureError(f"Fixture '{fixture_key}' must contain object payloads.")
    return source, expected


def _fixture_status(source: dict[str, Any], expected: dict[str, Any]) -> str:
    return str(expected.get("status") or source.get("status") or "incomplete").strip().lower()


def _last_result_for_fixture(fixture_key: str, status: str, skip_reason: str) -> dict[str, Any]:
    if status != "complete":
        return {"status": "SKIPPED", "skip_reason": skip_reason}
    try:
        result = run_fixture(fixture_key)
    except Exception as exc:  # pragma: no cover - defensive payload for UI diagnostics
        return {"status": "ERROR", "message": str(exc)}
    failed = [
        check["field"]
        for check in result.get("checks") or []
        if not check.get("pass") and check.get("required", True)
    ]
    return {
        "status": "PASS" if result.get("ok") else "FAIL",
        "failed_checks": failed,
    }


def _skip_reason(source: dict[str, Any], expected: dict[str, Any]) -> str:
    return str(
        source.get("skip_reason")
        or expected.get("skip_reason")
        or source.get("missing_source_reason")
        or expected.get("missing_source_reason")
        or expected.get("notes")
        or ""
    ).strip()


def _safe_key(value: str) -> str:
    key = str(value or "").strip()
    if not key or any(part in key for part in ("..", "/", "\\")):
        raise CanonicalFixtureError("Invalid canonical fixture key.")
    return key


def _actual_from_invoice(
    normalized: dict[str, Any],
    rows: list[dict[str, Any]],
    source: dict[str, Any],
) -> dict[str, Any]:
    line_items = [
        {
            "line_item_number": row.get("Line Item Number"),
            "gl_account": str(row.get("GL Account") or ""),
            "description": str(row.get("Line Item Description") or ""),
            "amount": _money(row.get("Amount")),
            "expense_type": str(row.get("Expense Type") or ""),
            "is_replacement_reserve": bool(row.get("Is Replacement Reserve")),
        }
        for row in rows
    ]
    return {
        "category": normalized.get("category"),
        "vendor": normalized.get("vendor_name"),
        "invoice_number": normalized.get("invoice_number"),
        "account_number": normalized.get("account_number") or "",
        "invoice_date": normalized.get("invoice_date"),
        "due_date": normalized.get("due_date") or "",
        "service_period": {
            "start": normalized.get("service_period_start") or "",
            "end": normalized.get("service_period_end") or "",
            "range": _service_period_range(normalized),
        },
        "property_abbreviation": normalized.get("property_abbreviation") or "",
        "property": normalized.get("property_abbreviation") or "",
        "location": normalized.get("location") or "",
        "location_policy": _category_location_policy(str(normalized.get("category") or "")),
        "invoice_description": rows[0].get("Invoice Description") if rows else "",
        "line_items": line_items,
        "gl_accounts": [item["gl_account"] for item in line_items],
        "line_amounts": [item["amount"] for item in line_items],
        "merchandise": _money(normalized.get("subtotal")),
        "tax": _money(normalized.get("tax_amount")),
        "total": _money(normalized.get("total_amount")),
        "review_flags": normalized.get("manual_review_codes") or [],
        "ignored_source_items": _ignored_source_items(normalized, rows, source),
        "total_reconciliation": (normalized.get("validation_summary") or {}).get("total_reconciliation_passed"),
    }


def _checks_from_expected(expected: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(field: str, expected_value: Any, actual_value: Any, *, reason: str = "", required: bool = True) -> None:
        checks.append(
            {
                "group": CHECK_GROUPS.get(field, "Other"),
                "field": field,
                "expected": expected_value,
                "actual": actual_value,
                "pass": _equivalent(expected_value, actual_value),
                "reason": reason,
                "required": required,
            }
        )

    mappings = [
        ("category", "category_expected", "category"),
        ("vendor", "vendor_expected", "vendor"),
        ("invoice_number", "invoice_number_expected", "invoice_number"),
        ("account_number", "account_number_expected", "account_number"),
        ("invoice_date", "invoice_date_expected", "invoice_date"),
        ("due_date", "due_date_expected", "due_date"),
        ("service_period", "service_period_expected", "service_period"),
        ("property_abbreviation", "property_abbreviation_expected", "property_abbreviation"),
        ("location", "location_expected", "location"),
        ("location_policy", "location_policy_expected", "location_policy"),
        ("invoice_description", "invoice_description_expected", "invoice_description"),
        ("line_items", "line_items_expected", "line_items"),
        ("merchandise", "merchandise_expected", "merchandise"),
        ("tax", "tax_expected", "tax"),
        ("total", "total_expected", "total"),
    ]
    for field, expected_key, actual_key in mappings:
        if expected_key in expected:
            add(field, expected.get(expected_key), actual.get(actual_key))

    if "expense_type_expected" in expected:
        add(
            "expense_type",
            expected["expense_type_expected"],
            sorted({item.get("expense_type") for item in actual.get("line_items") or []}),
        )
    if "is_replacement_reserve_expected" in expected:
        add(
            "is_replacement_reserve",
            expected["is_replacement_reserve_expected"],
            sorted({item.get("is_replacement_reserve") for item in actual.get("line_items") or []}),
        )
    if "expected_review_flags" in expected:
        expected_flags = expected.get("expected_review_flags") or []
        actual_flags = actual.get("review_flags") or []
        add(
            "review_flags",
            expected_flags,
            [flag for flag in expected_flags if flag in actual_flags],
            reason="Expected review flags must be present; additional flags are shown but do not fail the fixture.",
        )
    if "ignored_source_items" in expected:
        add("ignored_source_items", expected.get("ignored_source_items") or [], actual.get("ignored_source_items") or [])
    return checks


def _equivalent(expected: Any, actual: Any) -> bool:
    if not isinstance(expected, list) and isinstance(actual, list):
        if not actual:
            return expected in ("", None, [])
        return all(_equivalent(expected, item) for item in actual)
    if isinstance(expected, float) or isinstance(actual, float):
        try:
            return abs(float(expected) - float(actual)) <= 0.01
        except Exception:
            return False
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        return all(_equivalent(exp_item, act_item) for exp_item, act_item in zip(expected, actual))
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        for key, value in expected.items():
            if not _equivalent(value, actual.get(key)):
                return False
        return True
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(expected) == bool(actual)
    return _normalize_compare(expected) == _normalize_compare(actual)


def _normalize_compare(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize_compare(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_compare(item) for item in value]
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    return str(value or "").strip()


def _service_period_range(normalized: dict[str, Any]) -> str:
    start = _short_date(normalized.get("service_period_start"))
    end = _short_date(normalized.get("service_period_end"))
    return f"{start}-{end}" if start and end else ""


def _short_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            from datetime import datetime

            return datetime.strptime(text, fmt).strftime("%m/%d/%y")
        except ValueError:
            continue
    return text


def _category_location_policy(category: str) -> str:
    rules = canonical_rules.load_rules()
    return str(((rules.get("categories") or {}).get(category) or {}).get("location_policy") or "")


def _ignored_source_items(normalized: dict[str, Any], rows: list[dict[str, Any]], source: dict[str, Any]) -> list[str]:
    ignored: list[str] = []
    descriptions = " ".join(str(row.get("Line Item Description") or "").lower() for row in rows)
    for item in source.get("ignored_source_items") or []:
        key = str(item)
        if key == "zero_amount_lines":
            if int(normalized.get("zero_amount_lines_excluded") or 0) > 0:
                ignored.append(key)
        elif key in {"previous_balance", "payments"}:
            if "previous balance" not in descriptions and "payment" not in descriptions:
                ignored.append(key)
        else:
            ignored.append(key)
    return ignored


def _rules_used(normalized: dict[str, Any]) -> list[str]:
    category = normalized.get("category") or "unknown"
    return [
        f"category:{category}",
        "required_fields",
        "property_reference_match",
        "chart_of_accounts_validation",
        "canonical_description_formats",
        "total_reconciliation",
    ]


def _timeline(
    source: dict[str, Any],
    normalized: dict[str, Any],
    rows: list[dict[str, Any]],
    actual: dict[str, Any],
) -> list[dict[str, str]]:
    return [
        {"step": "Document read", "detail": f"Cached fixture candidates loaded from {source.get('source_document') or 'source_reference.json'}."},
        {"step": "Vendor detected", "detail": f"Vendor resolved to {actual.get('vendor') or 'not set'}."},
        {"step": "Category classified", "detail": f"Canonical category is {actual.get('category') or 'unknown'}."},
        {"step": "Canonical rules loaded", "detail": "Runtime config/canonical_rules.yaml was applied without editing the YAML."},
        {"step": "Property matched", "detail": f"Property resolved to {actual.get('property_abbreviation') or 'not set'}."},
        {"step": "GL selected", "detail": f"GL account(s): {', '.join(actual.get('gl_accounts') or []) or 'not set'}."},
        {"step": "Descriptions composed", "detail": "Invoice and line descriptions were generated by canonical templates."},
        {
            "step": "Totals reconciled",
            "detail": f"{len(rows)} payable row(s), invoice total {actual.get('total'):.2f}.",
        },
        {
            "step": "Review tasks generated",
            "detail": ", ".join(normalized.get("manual_review_codes") or []) or "No review flags.",
        },
    ]


def _money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except Exception:
        return 0.0


__all__ = [
    "CanonicalFixtureError",
    "list_fixtures",
    "run_all_complete",
    "run_fixture",
]
