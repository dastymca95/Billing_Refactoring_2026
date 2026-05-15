"""Canonical Rules Studio service.

This module is the safe, app-facing editing and test-bench layer around
``config/canonical_rules.yaml``. Runtime processing keeps using
``canonical_rules.py``; this service only exposes human-readable summaries,
small whitelisted edits, validation, restore, and dry-run tests.
"""

from __future__ import annotations

import copy
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .. import settings
from . import ai_mapping_review, canonical_invoice_fixtures, canonical_rules, template_rules


BACKUP_DIR = settings.PROJECT_ROOT / "config" / ".backups"

CATEGORY_ORDER = [
    "utilities",
    "pest_control",
    "landscaping",
    "marketing",
    "subscriptions",
    "trash_collection_services",
    "other_infrequent",
    "unknown",
]

LOCATION_POLICIES = {
    "valid_unit_if_present": "Use a known unit/location when present; otherwise review.",
    "optional_valid_unit_only": "Location is optional, but raw addresses are never written.",
    "property_level_blank_location_allowed": "Property-level service may leave Location blank.",
}

EDITABLE_CATEGORY_FIELDS = {
    "labels",
    "vendor_keywords",
    "service_keywords",
    "default_gl_candidates",
    "fee_handling",
    "ignore_line_keywords",
    "location_policy",
    "use_ai",
    "require_vendor_validation",
    "require_gl_validation",
}

EDITABLE_TEMPLATE_FIELDS = {
    "invoice_description_format",
    "line_item_description_format",
}

CAPITAL_WASTE_SAMPLE = {
    "vendor_name": "Capital Waste Services",
    "invoice_number": "3150854",
    "invoice_date": "04/30/2026",
    "due_date": "05/30/2026",
    "bill_or_credit": "Bill",
    "account_number": "160243",
    "service_address": "River Canyon Apartments, 21726 River Canyon Rd, Chattanooga, TN 37405",
    "service_period_start": "05/01/2026",
    "service_period_end": "05/31/2026",
    "property_candidate": "River Canyon Apartments",
    "line_items": [
        {
            "description": "6 Yard Trash Service",
            "quantity": 1,
            "unit_price": 365.40,
            "amount": 365.40,
            "confidence": 0.95,
            "reason": "Visible service line.",
        },
        {
            "description": "Fuel Recovery Adjustment",
            "quantity": 1,
            "unit_price": 34.93,
            "amount": 34.93,
            "confidence": 0.95,
            "reason": "Visible fee line.",
        },
    ],
    "subtotal": 400.33,
    "tax_amount": 0,
    "total_amount": 400.33,
    "confidence": 0.95,
    "warnings": [],
}

CAPITAL_WASTE_EXPECTED = {
    "category": "trash_collection_services",
    "vendor": "Capital Waste Services",
    "invoice_number": "3150854",
    "invoice_date": "04/30/2026",
    "due_date": "05/30/2026",
    "property": "RCC",
    "location": "",
    "gl_accounts": ["6940", "6940"],
    "line_amounts": [365.40, 34.93],
    "total": 400.33,
    "invoice_description": "05/01/26-05/31/26 - River Canyon Apartments",
    "line_descriptions": [
        "05/01/26-05/31/26 - River Canyon Apartments - 6 Yard Trash Service",
        "05/01/26-05/31/26 - River Canyon Apartments - Fuel Recovery Adjustment",
    ],
    "previous_balance_payment_ignored": True,
}


class CanonicalRulesStudioError(ValueError):
    """Raised when the Studio receives an unsafe or invalid request."""


def list_payload() -> dict[str, Any]:
    rules = _current_rules()
    categories = rules.get("categories") or {}
    ordered_keys = [key for key in CATEGORY_ORDER if key in categories] + [
        key for key in categories.keys() if key not in CATEGORY_ORDER
    ]
    return {
        "source": rules.get("source") or {},
        "required_columns": rules.get("template_requirements", {}).get("required_columns")
        or canonical_rules.REQUIRED_TEMPLATE_COLUMNS,
        "optional_columns": rules.get("template_requirements", {}).get("optional_columns")
        or canonical_rules.OPTIONAL_TEMPLATE_COLUMNS,
        "categories": [_category_summary(key, rules) for key in ordered_keys],
        "variables": _variables(),
    }


def category_payload(category: str) -> dict[str, Any]:
    rules = _current_rules()
    category = _require_category(category, rules)
    return {
        "category": _category_summary(category, rules),
        "groups": _human_rule_groups(category, rules),
        "editable": _editable_category(category, rules),
        "validation": validate_rules_config(rules),
    }


def validate_rules_config(rules: dict[str, Any] | None = None) -> dict[str, Any]:
    rules = copy.deepcopy(rules or _current_rules())
    issues: list[dict[str, str]] = []
    required = rules.get("template_requirements", {}).get("required_columns")
    if not isinstance(required, list) or not required:
        issues.append({
            "severity": "error",
            "path": "template_requirements.required_columns",
            "message": "At least one required template column must be configured.",
        })
    else:
        for column in canonical_rules.REQUIRED_TEMPLATE_COLUMNS:
            if column not in required:
                issues.append({
                    "severity": "error",
                    "path": "template_requirements.required_columns",
                    "message": f"{column} is mandatory for this project and cannot be removed.",
                })

    categories = rules.get("categories")
    if not isinstance(categories, dict) or not categories:
        issues.append({
            "severity": "error",
            "path": "categories",
            "message": "Canonical categories are missing.",
        })
        categories = {}

    for category in CATEGORY_ORDER:
        if category not in categories:
            issues.append({
                "severity": "error",
                "path": f"categories.{category}",
                "message": f"Required category '{category}' is missing.",
            })

    for category, cfg in categories.items():
        if not isinstance(cfg, dict):
            issues.append({
                "severity": "error",
                "path": f"categories.{category}",
                "message": "Category settings must be an object.",
            })
            continue
        policy = str(cfg.get("location_policy") or "").strip()
        if policy and policy not in LOCATION_POLICIES:
            issues.append({
                "severity": "error",
                "path": f"categories.{category}.location_policy",
                "message": f"Unknown location policy '{policy}'.",
            })
        defaults = cfg.get("default_gl_candidates") or {}
        if defaults and not isinstance(defaults, dict):
            issues.append({
                "severity": "error",
                "path": f"categories.{category}.default_gl_candidates",
                "message": "Default GL candidates must be key/value pairs.",
            })
        for key, gl_code in (defaults or {}).items():
            text = str(gl_code or "").strip()
            if not text:
                continue
            if not ai_mapping_review.validate_gl_account(text):
                issues.append({
                    "severity": "warning",
                    "path": f"categories.{category}.default_gl_candidates.{key}",
                    "message": f"GL '{text}' is not a valid Chart of Accounts code.",
                })

    formats = rules.get("template_columns", {})
    for field in ("invoice_description", "line_item_description"):
        category_formats = (formats.get(field) or {}).get("formats") or {}
        for category in (rules.get("categories") or {}).keys():
            if not isinstance(category_formats.get(category, ""), str):
                issues.append({
                    "severity": "error",
                    "path": f"template_columns.{field}.formats.{category}",
                    "message": "Description format must be text.",
                })

    return {
        "ok": not any(issue["severity"] == "error" for issue in issues),
        "issues": issues,
    }


def validate_request(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    if isinstance(body.get("config"), dict):
        return validate_rules_config(body["config"])
    rules = _current_rules()
    category = body.get("category")
    patch = body.get("patch")
    if category and isinstance(patch, dict):
        draft = apply_patch_to_rules(rules, str(category), patch)
        return validate_rules_config(draft)
    return validate_rules_config(rules)


def apply_category_patch(category: str, patch: dict[str, Any]) -> dict[str, Any]:
    rules = _current_rules()
    category = _require_category(category, rules)
    _validate_patched_gl_values(category, patch or {})
    draft = apply_patch_to_rules(rules, category, patch or {})
    validation = validate_rules_config(draft)
    if not validation["ok"]:
        raise CanonicalRulesStudioError(_validation_message(validation))
    backup = _backup_current_rules()
    _write_rules(draft)
    return {
        "ok": True,
        "backup_path": str(backup),
        "category": category,
        "validation": validation,
    }


def apply_patch_to_rules(rules: dict[str, Any], category: str, patch: dict[str, Any]) -> dict[str, Any]:
    draft = copy.deepcopy(rules)
    category = _require_category(category, draft)
    cfg = draft.setdefault("categories", {}).setdefault(category, {})
    for key, value in patch.items():
        if key in EDITABLE_CATEGORY_FIELDS:
            cfg[key] = _normalize_edit_value(key, value)
            continue
        if key == "invoice_description_format":
            _set_format(draft, "invoice_description", category, str(value or ""))
            continue
        if key == "line_item_description_format":
            _set_format(draft, "line_item_description", category, str(value or ""))
            continue
        raise CanonicalRulesStudioError(f"'{key}' is not editable from Canonical Rules Studio.")
    draft.setdefault("source", {})["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return draft


def _validate_patched_gl_values(category: str, patch: dict[str, Any]) -> None:
    defaults = patch.get("default_gl_candidates")
    if defaults is None:
        return
    if not isinstance(defaults, dict):
        raise CanonicalRulesStudioError("Default GL candidates must be key/value pairs.")
    for key, gl_code in defaults.items():
        text = str(gl_code or "").strip()
        if text and not ai_mapping_review.validate_gl_account(text):
            raise CanonicalRulesStudioError(
                f"GL '{text}' for '{category}.{key}' is not a valid Chart of Accounts code."
            )


def restore_latest_backup() -> dict[str, Any]:
    backups = sorted(BACKUP_DIR.glob("canonical_rules_*.yaml"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not backups:
        raise CanonicalRulesStudioError("No canonical rules backup is available.")
    latest = backups[0]
    canonical_rules.CANONICAL_RULES_YAML.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(latest, canonical_rules.CANONICAL_RULES_YAML)
    _reset_runtime_caches()
    return {"ok": True, "restored_from": str(latest)}


def import_preview_from_excel() -> dict[str, Any]:
    try:
        imported = canonical_rules.import_canonical_rules_from_excel(write=False)
    except Exception as exc:
        raise CanonicalRulesStudioError(str(exc)) from exc
    current = _current_rules()
    return {
        "ok": True,
        "excel_path": str(canonical_rules.CANONICAL_RULES_XLSX),
        "changed_categories": _changed_categories(current, imported),
        "imported_rows": len(imported.get("source", {}).get("imported_rows") or []),
        "validation": validate_rules_config(imported),
    }


def apply_import_from_excel() -> dict[str, Any]:
    preview = import_preview_from_excel()
    if not preview["validation"]["ok"]:
        raise CanonicalRulesStudioError(_validation_message(preview["validation"]))
    backup = _backup_current_rules()
    try:
        canonical_rules.import_canonical_rules_from_excel(write=True)
    except Exception as exc:
        raise CanonicalRulesStudioError(str(exc)) from exc
    return {"ok": True, "backup_path": str(backup), "preview": preview}


def list_test_fixtures() -> dict[str, Any]:
    return canonical_invoice_fixtures.list_fixtures()


def run_test_bench(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    rules = _current_rules()
    draft_patch = body.get("draft_patch")
    category = str(body.get("category") or "trash_collection_services")
    if isinstance(draft_patch, dict) and draft_patch:
        rules = apply_patch_to_rules(rules, category, draft_patch)
        dry_run = True
    else:
        dry_run = False

    if body.get("run_all"):
        return canonical_invoice_fixtures.run_all_complete(rules_override=rules)

    fixture_key = str(body.get("fixture_key") or body.get("test_case") or "capital_waste")
    try:
        return canonical_invoice_fixtures.run_fixture(
            fixture_key,
            rules_override=rules,
            dry_run=dry_run,
        )
    except canonical_invoice_fixtures.CanonicalFixtureError as exc:
        raise CanonicalRulesStudioError(str(exc)) from exc


def _capital_waste_actual(normalized: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    descriptions = [str(row.get("Line Item Description") or "") for row in rows]
    return {
        "category": normalized.get("category"),
        "vendor": normalized.get("vendor_name"),
        "invoice_number": normalized.get("invoice_number"),
        "invoice_date": normalized.get("invoice_date"),
        "due_date": normalized.get("due_date"),
        "property": normalized.get("property_abbreviation"),
        "location": normalized.get("location") or "",
        "gl_accounts": [str(row.get("GL Account") or "") for row in rows],
        "line_amounts": [round(float(row.get("Amount") or 0), 2) for row in rows],
        "total": round(float(normalized.get("total_amount") or 0), 2),
        "invoice_description": rows[0].get("Invoice Description") if rows else "",
        "line_descriptions": descriptions,
        "previous_balance_payment_ignored": "payment" not in " ".join(descriptions).lower(),
    }


def _checks(expected: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        checks.append({
            "field": key,
            "expected": expected_value,
            "actual": actual_value,
            "pass": actual_value == expected_value,
        })
    return checks


def _capital_waste_timeline(normalized: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    location_note = (
        "Location left blank because trash_collection_services allows property-level service when no unit is provided."
        if not normalized.get("location")
        else f"Location matched to {normalized.get('location')}."
    )
    return [
        {"step": "Document read", "detail": "Built-in Capital Waste invoice candidates loaded for dry-run testing."},
        {"step": "Vendor detected", "detail": f"Vendor candidate resolved to {normalized.get('vendor_name')}."},
        {"step": "Category classified", "detail": "Waste/trash keywords classified the invoice as trash_collection_services."},
        {"step": "Canonical rules loaded", "detail": "Trash rules require GL, property, due date, document URL, and service-period descriptions."},
        {"step": "Property matched", "detail": f"River Canyon Apartments matched to {normalized.get('property_abbreviation')}."},
        {"step": "Location policy", "detail": location_note},
        {"step": "GL selected", "detail": "Trash service and fuel recovery use GL 6940 by category default."},
        {"step": "Descriptions composed", "detail": "Invoice and line descriptions were rendered from service period + property/site + line item."},
        {"step": "Totals reconciled", "detail": f"{len(rows)} payable row(s) sum to the invoice total."},
        {"step": "Review tasks generated", "detail": "Only genuine unresolved validations remain as review flags."},
    ]


def _category_summary(category: str, rules: dict[str, Any]) -> dict[str, Any]:
    cfg = (rules.get("categories") or {}).get(category) or {}
    label = _label_for(category, cfg)
    groups = _human_rule_groups(category, rules)
    summary = []
    for group in groups[:4]:
        if group["items"]:
            summary.append(group["items"][0])
    return {
        "key": category,
        "label": label,
        "summary": summary,
        "group_count": len(groups),
        "editable": True,
    }


def _human_rule_groups(category: str, rules: dict[str, Any]) -> list[dict[str, Any]]:
    cfg = (rules.get("categories") or {}).get(category) or {}
    req = rules.get("template_requirements", {}).get("required_columns") or canonical_rules.REQUIRED_TEMPLATE_COLUMNS
    invoice_fmt = _get_format(rules, "invoice_description", category)
    line_fmt = _get_format(rules, "line_item_description", category)
    location_policy = str(cfg.get("location_policy") or "optional_valid_unit_only")
    defaults = cfg.get("default_gl_candidates") or {}
    fee_handling = cfg.get("fee_handling") or {}
    ignore_lines = cfg.get("ignore_line_keywords") or []
    doc_policy = rules.get("template_requirements", {}).get("document_url_policy") or "required_when_dropbox_available"
    return [
        {
            "key": "identity",
            "title": "Identity / category detection",
            "items": [
                f"Labels: {', '.join(cfg.get('labels') or [_label_for(category, cfg)])}",
                _words("Vendor keywords", cfg.get("vendor_keywords") or []),
                _words("Service keywords", cfg.get("service_keywords") or []),
            ],
        },
        {
            "key": "required_fields",
            "title": "Required fields",
            "items": [
                "Rows cannot be ready to export until mandatory ResMan fields are filled.",
                ", ".join(req),
            ],
        },
        {
            "key": "dates",
            "title": "Date rules",
            "items": [
                "Invoice date priority: explicit invoice date, statement date, purchase date, ship date.",
                "Accounting date defaults to invoice date.",
                "Due date priority: explicit due date, autopay date, then terms plus invoice date.",
            ],
        },
        {
            "key": "descriptions",
            "title": "Description rules",
            "items": [
                f"Invoice description: {invoice_fmt}",
                f"Line item description: {line_fmt}",
                "AI descriptions are treated as candidates; final wording is composed by Canonical Rules.",
            ],
        },
        {
            "key": "property_location",
            "title": "Property / location rules",
            "items": [
                "Property Abbreviation must come from known property references.",
                LOCATION_POLICIES.get(location_policy, location_policy),
                "Raw full addresses are never written into Location.",
            ],
        },
        {
            "key": "gl_mapping",
            "title": "GL mapping rules",
            "items": [
                "GL Account must be a numeric code validated against the Chart of Accounts.",
                _gl_defaults(defaults),
                "Learned mappings can help, but cannot override impossible validation.",
            ],
        },
        {
            "key": "tax_fee",
            "title": "Tax / fee handling",
            "items": [
                _fee_handling(fee_handling),
                "Totals are reconciled before rows are marked ready.",
                "Fuel/recovery/environmental fees follow category rules when configured.",
            ],
        },
        {
            "key": "exclusions",
            "title": "Previous balance / payment exclusion",
            "items": [
                _words("Ignored line keywords", ignore_lines),
                "Previous balances, payments, remittance, autopay, and amount enclosed lines are not expense rows.",
            ],
        },
        {
            "key": "document_url",
            "title": "Document URL rules",
            "items": [
                f"Document URL policy: {doc_policy}.",
                "Local dry-runs may flag a missing Dropbox link, but export readiness requires the configured policy.",
            ],
        },
        {
            "key": "manual_review",
            "title": "Manual review triggers",
            "items": [
                "Missing vendor, property, GL, invoice number, dates, amount, or document URL creates blocking review tasks.",
                "Low confidence, text/vision conflicts, inferred dates, and total mismatches stay reviewable.",
            ],
        },
    ]


def _editable_category(category: str, rules: dict[str, Any]) -> dict[str, Any]:
    cfg = (rules.get("categories") or {}).get(category) or {}
    return {
        "labels": list(cfg.get("labels") or []),
        "vendor_keywords": list(cfg.get("vendor_keywords") or []),
        "service_keywords": list(cfg.get("service_keywords") or []),
        "default_gl_candidates": dict(cfg.get("default_gl_candidates") or {}),
        "fee_handling": dict(cfg.get("fee_handling") or {}),
        "ignore_line_keywords": list(cfg.get("ignore_line_keywords") or []),
        "location_policy": cfg.get("location_policy") or "optional_valid_unit_only",
        "use_ai": bool(cfg.get("use_ai", False)),
        "require_vendor_validation": bool(cfg.get("require_vendor_validation", False)),
        "require_gl_validation": bool(cfg.get("require_gl_validation", False)),
        "invoice_description_format": _get_format(rules, "invoice_description", category),
        "line_item_description_format": _get_format(rules, "line_item_description", category),
    }


def _current_rules() -> dict[str, Any]:
    return copy.deepcopy(canonical_rules.load_rules())


def _require_category(category: str, rules: dict[str, Any]) -> str:
    key = str(category or "").strip()
    if key not in (rules.get("categories") or {}):
        raise CanonicalRulesStudioError("Invalid canonical rules category.")
    return key


def _normalize_edit_value(key: str, value: Any) -> Any:
    if key in {"labels", "vendor_keywords", "service_keywords", "ignore_line_keywords"}:
        if isinstance(value, str):
            return [line.strip() for line in value.replace(",", "\n").splitlines() if line.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []
    if key in {"default_gl_candidates", "fee_handling"}:
        return dict(value or {}) if isinstance(value, dict) else {}
    if key in {"use_ai", "require_vendor_validation", "require_gl_validation"}:
        return bool(value)
    return str(value or "").strip()


def _set_format(rules: dict[str, Any], field: str, category: str, value: str) -> None:
    rules.setdefault("template_columns", {}).setdefault(field, {}).setdefault("formats", {})[category] = value


def _get_format(rules: dict[str, Any], field: str, category: str) -> str:
    return str(
        ((rules.get("template_columns") or {}).get(field) or {}).get("formats", {}).get(category)
        or ((rules.get("template_columns") or {}).get(field) or {}).get("formats", {}).get("unknown")
        or ""
    )


def _backup_current_rules() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    source = canonical_rules.CANONICAL_RULES_YAML
    if not source.is_file():
        source.parent.mkdir(parents=True, exist_ok=True)
        _write_rules(_current_rules(), reset=False)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_DIR / f"canonical_rules_{stamp}.yaml"
    shutil.copy2(source, target)
    return target


def _write_rules(rules: dict[str, Any], *, reset: bool = True) -> None:
    path = canonical_rules.CANONICAL_RULES_YAML
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp")
    tmp_path = Path(handle.name)
    try:
        with handle:
            yaml.safe_dump(rules, handle, sort_keys=False, allow_unicode=False, width=120)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    if reset:
        _reset_runtime_caches()


def _reset_runtime_caches() -> None:
    canonical_rules.reset_cache()
    template_rules.reset_cache()


def _changed_categories(current: dict[str, Any], imported: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    current_cats = current.get("categories") or {}
    imported_cats = imported.get("categories") or {}
    for key in sorted(set(current_cats) | set(imported_cats)):
        if current_cats.get(key) != imported_cats.get(key):
            changed.append(key)
    return changed


def _validation_message(validation: dict[str, Any]) -> str:
    issue = next((item for item in validation.get("issues") or [] if item.get("severity") == "error"), None)
    if not issue:
        return "Canonical rules validation failed."
    return str(issue.get("message") or "Canonical rules validation failed.")


def _label_for(category: str, cfg: dict[str, Any]) -> str:
    labels = cfg.get("labels") or []
    return str(labels[0]) if labels else category.replace("_", " ").title()


def _words(label: str, values: list[Any]) -> str:
    clean = [str(item).strip() for item in values if str(item).strip()]
    return f"{label}: {', '.join(clean) if clean else 'none configured'}"


def _gl_defaults(defaults: dict[str, Any]) -> str:
    if not defaults:
        return "No category default GL is configured."
    return "Default GL candidates: " + ", ".join(f"{k} -> {v}" for k, v in defaults.items())


def _fee_handling(fee_handling: dict[str, Any]) -> str:
    if not fee_handling:
        return "No category-specific fee mapping is configured."
    return "Fee handling: " + ", ".join(f"{k} -> {v}" for k, v in fee_handling.items())


def _variables() -> list[dict[str, str]]:
    return [
        {"key": "service_period", "label": "{service_period}"},
        {"key": "property_or_site_name", "label": "{property_or_site_name}"},
        {"key": "service_address_or_property", "label": "{service_address_or_property}"},
        {"key": "vendor_name", "label": "{vendor_name}"},
        {"key": "invoice_date_short", "label": "{invoice_date_short}"},
        {"key": "line_item_description", "label": "{line_item_description}"},
        {"key": "line_item_description_short", "label": "{line_item_description_short}"},
        {"key": "main_item_or_category", "label": "{main_item_or_category}"},
        {"key": "amount", "label": "{amount}"},
    ]


__all__ = [
    "CanonicalRulesStudioError",
    "apply_category_patch",
    "apply_import_from_excel",
    "category_payload",
    "import_preview_from_excel",
    "list_test_fixtures",
    "list_payload",
    "restore_latest_backup",
    "run_test_bench",
    "validate_request",
    "validate_rules_config",
]
