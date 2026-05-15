"""Canonical invoice rules and universal reasoning helpers.

This layer turns the operator-authored Canonica rules workbook into runtime
rules for AI-assisted invoices. It deliberately sits after AI extraction and
before ResMan row construction: AI returns candidates, this module applies
business rules, internal references, and required-field validation.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .. import settings
from . import ai_mapping_review
from utils.text_normalization import (
    normalize_service_address_for_description,
    proper_case_preserve_acronyms,
)


CANONICAL_RULES_XLSX = settings.PROJECT_ROOT / "Canonica rules.xlsx"
CANONICAL_RULES_YAML = settings.PROJECT_ROOT / "config" / "canonical_rules.yaml"


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


COLUMN_KEY_TO_HEADER: dict[str, str] = {
    "invoice_number": "Invoice Number",
    "bill_or_credit": "Bill or Credit",
    "invoice_date": "Invoice Date",
    "accounting_date": "Accounting Date",
    "vendor": "Vendor",
    "invoice_description": "Invoice Description",
    "line_item_number": "Line Item Number",
    "property_abbreviation": "Property Abbreviation",
    "location": "Location",
    "gl_account": "GL Account",
    "line_item_description": "Line Item Description",
    "amount": "Amount",
    "expense_type": "Expense Type",
    "is_replacement_reserve": "Is Replacement Reserve",
    "payment_date": "Payment Date",
    "reference_number": "Reference Number",
    "payment_method": "Payment Method",
    "department": "Department",
    "due_date": "Due Date",
    "quantity": "Quantity",
    "unit_price": "Unit Price",
    "tax": "Tax",
    "received_date": "Received Date",
    "document_url": "Document Url",
}

HEADER_TO_COLUMN_KEY = {v.lower(): k for k, v in COLUMN_KEY_TO_HEADER.items()}

REQUIRED_TEMPLATE_COLUMNS: list[str] = [
    "Invoice Number",
    "Bill or Credit",
    "Invoice Date",
    "Accounting Date",
    "Vendor",
    "Invoice Description",
    "Line Item Number",
    "Property Abbreviation",
    "GL Account",
    "Line Item Description",
    "Amount",
    "Expense Type",
    "Is Replacement Reserve",
    "Due Date",
    "Document Url",
]

OPTIONAL_TEMPLATE_COLUMNS: list[str] = [
    "Location",
    "Payment Date",
    "Reference Number",
    "Payment Method",
    "Department",
    "Quantity",
    "Unit Price",
    "Tax",
    "Received Date",
]


def default_rules() -> dict[str, Any]:
    return {
        "version": 1,
        "source": {
            "excel_path": str(CANONICAL_RULES_XLSX),
            "runtime_source": str(CANONICAL_RULES_YAML),
            "notes": "Generated from Canonica rules.xlsx and completed with runtime defaults.",
        },
        "template_requirements": {
            "required_columns": REQUIRED_TEMPLATE_COLUMNS,
            "optional_columns": OPTIONAL_TEMPLATE_COLUMNS,
            "location_policy": "optional_valid_unit_only",
            "document_url_policy": "required_when_dropbox_available",
        },
        "template_columns": {
            "invoice_number": {
                "required": True,
                "source_priority": ["canonical_category_rule", "invoice_number_from_bill", "account_number_period_format"],
                "validation": {"cannot_be_blank": True},
            },
            "bill_or_credit": {
                "required": True,
                "default": "Bill",
                "credit_detection_keywords": ["credit memo", "credit", "refund"],
            },
            "invoice_date": {
                "required": True,
                "source_priority": ["explicit_invoice_date", "statement_date", "purchase_date", "ship_date"],
                "if_inferred": "flag_review",
            },
            "accounting_date": {"required": True, "default_rule": "same_as_invoice_date"},
            "vendor": {
                "required": True,
                "source": "vendor_list_exact_or_candidate_match",
                "if_unmatched": "vendor_mapping_required",
            },
            "invoice_description": {
                "required": True,
                "never_use_vague_ai_description": True,
                "formats": {
                    "utilities": "{service_period} - {service_address_or_property}",
                    "trash_collection_services": "{service_period} - {property_or_site_name}",
                    "pest_control": "{service_period} - {property_or_site_name}",
                    "landscaping": "{service_period} - {property_or_site_name}",
                    "marketing": "{invoice_date_short} - {vendor_name} - {main_item_or_category}",
                    "subscriptions": "{invoice_date_short} - {vendor_name} - {main_item_or_category}",
                    "other_infrequent": "{invoice_date_short} - {vendor_name} - {main_item_or_category}",
                    "unknown": "{invoice_date_short} - {vendor_name} - {main_item_or_category}",
                },
            },
            "line_item_number": {"required": True, "rule": "sequential_per_invoice"},
            "property_abbreviation": {
                "required": True,
                "source": "property_reference_match",
                "if_unmatched": "property_mapping_required",
            },
            "location": {
                "required": False,
                "source": "unit_info_valid_location_only",
                "never_use_raw_address": True,
            },
            "gl_account": {
                "required": True,
                "source": "chart_of_accounts_valid_numeric_only",
                "if_unmatched": "gl_mapping_required",
            },
            "line_item_description": {
                "required": True,
                "formats": {
                    "utilities": "{service_period} - {service_address_or_property} - {line_item_description}",
                    "trash_collection_services": "{service_period} - {property_or_site_name} - {line_item_description}",
                    "pest_control": "{service_period} - {property_or_site_name} - {line_item_description}",
                    "landscaping": "{service_period} - {property_or_site_name} - {line_item_description}",
                    "marketing": "{vendor_name} - {line_item_description}",
                    "subscriptions": "{vendor_name} - {line_item_description}",
                    "other_infrequent": "{line_item_description}",
                    "unknown": "{vendor_name} - {line_item_description}",
                },
            },
            "amount": {"required": True, "validation": {"numeric": True, "reconcile_to_invoice_total": True}},
            "expense_type": {"required": True, "default": "General"},
            "is_replacement_reserve": {"required": True, "default": False},
            "due_date": {"required": True, "source_priority": ["explicit_due_date", "autopay_date", "terms_plus_invoice_date"]},
            "document_url": {"required": True, "source": "dropbox_after_upload"},
        },
        "categories": {
            "utilities": {
                "labels": ["Utilities"],
                "vendor_keywords": ["utility", "utilities", "water", "electric", "wastewater", "fiber", "internet", "gas", "power"],
                "service_keywords": ["water", "sewer", "electric", "internet", "fiber", "gas", "wastewater"],
                "default_gl_candidates": {
                    "water": "6955",
                    "sewer": "6955",
                    "wastewater": "6955",
                    "internet": "6920",
                    "fiber": "6920",
                    "electric": "6950",
                    "gas": "6995",
                },
                "invoice_number_rule": "account_number_service_period",
                "location_policy": "valid_unit_if_present",
            },
            "pest_control": {
                "labels": ["Pest Control"],
                "vendor_keywords": ["pest", "termite", "exterminator"],
                "default_gl_candidates": {"default": "6760"},
                "location_policy": "property_level_blank_location_allowed",
            },
            "landscaping": {
                "labels": ["Landscaping"],
                "vendor_keywords": ["landscap", "lawn", "mulch", "tree"],
                "default_gl_candidates": {"default": "6750"},
                "location_policy": "property_level_blank_location_allowed",
            },
            "marketing": {
                "labels": ["Marketing"],
                "vendor_keywords": ["marketing", "advertising", "apartments.com", "costar"],
                "default_gl_candidates": {"default": "6630"},
                "location_policy": "property_level_blank_location_allowed",
            },
            "subscriptions": {
                "labels": ["Subscriptions", "Suscriptions"],
                "vendor_keywords": ["subscription", "software", "license", "saas"],
                "default_gl_candidates": {"default": "6665"},
                "location_policy": "property_level_blank_location_allowed",
            },
            "trash_collection_services": {
                "labels": ["Trash collections services"],
                "vendor_keywords": ["waste", "trash", "garbage", "refuse", "dumpster", "capital waste", "waste connections"],
                "service_keywords": ["trash", "waste", "container", "dumpster", "fuel recovery", "removal"],
                "default_gl_candidates": {"default": "6940", "fuel_recovery": "6940", "environmental": "6940"},
                "fee_handling": {"fuel_recovery_adjustment": "same_gl_as_service"},
                "ignore_line_keywords": ["previous balance", "balance forward", "payment received", "autopay", "amount enclosed"],
                "location_policy": "property_level_blank_location_allowed",
            },
            "other_infrequent": {
                "labels": ["Other infrequent / maintenance / supplies / purchases"],
                "vendor_keywords": ["supply", "supplies", "maintenance", "repair", "materials", "appliance", "parts"],
                "use_ai": True,
                "require_vendor_validation": True,
                "require_gl_validation": True,
                "location_policy": "valid_unit_if_present",
            },
            "unknown": {
                "labels": ["Unknown"],
                "vendor_keywords": [],
                "use_ai": True,
                "location_policy": "valid_unit_if_present",
            },
        },
    }


@lru_cache(maxsize=1)
def load_rules() -> dict[str, Any]:
    rules = default_rules()
    if CANONICAL_RULES_YAML.is_file():
        try:
            loaded = yaml.safe_load(CANONICAL_RULES_YAML.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                rules = _merge_dicts(rules, loaded)
        except Exception:
            return rules
    return rules


def reset_cache() -> None:
    load_rules.cache_clear()


def import_canonical_rules_from_excel(
    xlsx_path: Path | None = None,
    yaml_path: Path | None = None,
    *,
    write: bool = True,
) -> dict[str, Any]:
    """Import the operator workbook into editable runtime YAML."""
    from openpyxl import load_workbook

    xlsx_path = Path(xlsx_path or CANONICAL_RULES_XLSX)
    yaml_path = Path(yaml_path or CANONICAL_RULES_YAML)
    if not xlsx_path.is_file():
        raise FileNotFoundError(f"Canonical rules workbook not found: {xlsx_path}")

    rules = default_rules()
    wb = load_workbook(xlsx_path, data_only=True, read_only=True)
    ws = wb.active
    category_row = None
    for row_index, row in enumerate(ws.iter_rows(), start=1):
        values = [str(cell.value or "").strip() for cell in row]
        if any(_category_key(value) == "utilities" for value in values):
            category_row = row_index
            break
    if not category_row:
        wb.close()
        raise ValueError("Could not find category header row in Canonica rules.xlsx.")

    categories_by_col: dict[int, str] = {}
    for cell in ws[category_row]:
        label = str(cell.value or "").strip()
        key = _category_key(label)
        if key:
            categories_by_col[cell.column] = key
            rules["categories"].setdefault(key, {"labels": [label], "vendor_keywords": []})
            labels = rules["categories"][key].setdefault("labels", [])
            if label and label not in labels:
                labels.append(label)

    imported_rows: list[dict[str, Any]] = []
    for row in ws.iter_rows(min_row=category_row + 1):
        label = str(row[1].value or "").strip() if len(row) >= 2 else ""
        if not label:
            continue
        col_key = _column_key(label)
        if not col_key:
            continue
        mandatory = "mandatory" in str(row[2].value or "").lower() if len(row) >= 3 else False
        col_rule = rules["template_columns"].setdefault(col_key, {})
        if mandatory:
            col_rule["required"] = True
        matrix: dict[str, str] = {}
        for column_index, cell in enumerate(row, start=1):
            cat_key = categories_by_col.get(column_index)
            if not cat_key:
                continue
            text = str(cell.value or "").strip()
            if text:
                matrix[cat_key] = text
        if matrix:
            col_rule["canonical_matrix"] = matrix
        imported_rows.append({
            "column": label,
            "key": col_key,
            "required": mandatory,
            "rules_by_category": matrix,
        })
    wb.close()

    rules["source"]["imported_rows"] = imported_rows
    rules["source"]["excel_path"] = str(xlsx_path)
    rules["source"]["runtime_source"] = str(yaml_path)

    if write:
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text(
            yaml.dump(rules, Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=False, width=120),
            encoding="utf-8",
        )
        reset_cache()
    return rules


def prompt_rules_summary() -> str:
    rules = load_rules()
    categories = rules.get("categories") or {}
    required = rules.get("template_requirements", {}).get("required_columns") or REQUIRED_TEMPLATE_COLUMNS
    category_lines = []
    for key, cfg in categories.items():
        labels = ", ".join(cfg.get("labels") or [key])
        keywords = ", ".join((cfg.get("vendor_keywords") or cfg.get("service_keywords") or [])[:8])
        category_lines.append(f"- {key}: labels={labels}; keywords={keywords}")
    return "\n".join(
        [
            "Canonical ResMan rules:",
            "AI returns extraction candidates only. The backend applies final ResMan formatting and validation.",
            "Required fields before export: " + ", ".join(required),
            "Never put a raw full address in Location. Location must be a valid unit/location or blank.",
            "GL Account must be a numeric code from the Chart of Accounts; vendor-side category text is source text only.",
            "Invoice Description and Line Item Description must follow the category-specific canonical formats.",
            "Ignore previous balances, payments, autopay, amount enclosed, and remittance lines as expenses.",
            "Categories:",
            *category_lines,
        ]
    )


def canonicalize_normalized_invoice(
    normalized: dict[str, Any],
    *,
    references: dict[str, list[dict[str, Any]]] | None = None,
    rules_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply canonical rules to an already validated AI extraction."""
    out = copy.deepcopy(normalized)
    rules = copy.deepcopy(rules_override) if rules_override else load_rules()
    category = classify_invoice_category(out, rules=rules)
    out["category"] = category
    out["category_label"] = _category_label(category, rules)

    _apply_vendor_candidate(out)
    _apply_property_context(out, category, rules)
    _apply_category_invoice_number(out, category, rules)
    _apply_gl_rules(out, category, rules)
    _apply_canonical_descriptions(out, category, rules)
    _apply_required_field_validation(out, rules)

    summary = dict(out.get("validation_summary") or {})
    summary["canonical_rules_applied"] = True
    summary["category"] = category
    summary["required_columns"] = rules.get("template_requirements", {}).get("required_columns", REQUIRED_TEMPLATE_COLUMNS)
    summary["blocking_required_fields"] = out.get("blocking_required_fields", [])
    out["validation_summary"] = summary
    return out


def classify_invoice_category(normalized: dict[str, Any], *, rules: dict[str, Any] | None = None) -> str:
    rules = rules or load_rules()
    explicit = _category_key(str(normalized.get("category") or ""))
    if explicit and explicit in rules.get("categories", {}):
        return explicit

    vendor_name = str(normalized.get("vendor_name") or normalized.get("raw_vendor_name") or "")
    vendor_rule_category = _vendor_yaml_category(vendor_name)
    if vendor_rule_category:
        mapped = _category_key(vendor_rule_category)
        if mapped in rules.get("categories", {}):
            return mapped

    haystack = " ".join(
        [
            vendor_name,
            str(normalized.get("invoice_description") or ""),
            str(normalized.get("service_address") or ""),
            " ".join(str(item.get("description") or "") for item in normalized.get("line_items") or [] if isinstance(item, dict)),
        ]
    ).lower()
    best_key = "unknown"
    best_score = 0
    for key, cfg in (rules.get("categories") or {}).items():
        score = 0
        for word in (cfg.get("vendor_keywords") or []) + (cfg.get("service_keywords") or []):
            word = str(word or "").lower().strip()
            if word and word in haystack:
                score += 2 if key == "trash_collection_services" and word in {"waste", "trash", "capital waste"} else 1
        if score > best_score:
            best_key, best_score = key, score
    return best_key if best_score else "unknown"


def required_columns() -> list[str]:
    return list(load_rules().get("template_requirements", {}).get("required_columns") or REQUIRED_TEMPLATE_COLUMNS)


def _apply_vendor_candidate(out: dict[str, Any]) -> None:
    vendor = str(out.get("vendor_name") or out.get("raw_vendor_name") or "").strip()
    if not vendor:
        return
    try:
        candidates = ai_mapping_review.vendor_candidates(vendor, limit=3)
    except Exception:
        return
    out["vendor_candidates"] = candidates.get("candidates") or []
    top = out["vendor_candidates"][0] if out["vendor_candidates"] else None
    if top and float(top.get("score") or 0) >= 0.90:
        out["vendor_name"] = top.get("vendor_name") or vendor


def _apply_property_context(out: dict[str, Any], category: str, rules: dict[str, Any]) -> None:
    if out.get("property_abbreviation"):
        if _category_allows_blank_location(category, rules) and _looks_like_raw_address(str(out.get("location") or "")):
            out["location"] = ""
        return
    query = str(out.get("property_candidate") or "")
    service_address = str(out.get("service_address") or "")
    try:
        candidates = ai_mapping_review.property_candidates(query=query, service_address=service_address, limit=5)
    except Exception:
        candidates = {"candidates": []}
    out["property_candidates"] = candidates.get("candidates") or []
    top = out["property_candidates"][0] if out["property_candidates"] else None
    if top and float(top.get("score") or 0) >= 0.78:
        out["property_abbreviation"] = top.get("property_abbreviation") or ""
        if _category_allows_blank_location(category, rules):
            out["location"] = ""
        else:
            out["location"] = top.get("location") or ""
        out["property_match"] = top
        _remove_issue(out, "property_mapping_required")
        if out.get("location"):
            _remove_issue(out, "location_unresolved")
    elif _category_allows_blank_location(category, rules):
        out["location"] = ""
    elif _looks_like_raw_address(str(out.get("location") or "")):
        out["location"] = ""


def _apply_category_invoice_number(out: dict[str, Any], category: str, rules: dict[str, Any]) -> None:
    source_invoice = str(out.get("source_invoice_number") or "").strip()
    if category != "utilities" and source_invoice:
        out["invoice_number"] = source_invoice
        out["invoice_number_policy_applied"] = False
        _remove_issue(out, "invoice_number_formatted_from_policy")
        _remove_issue(out, "invoice_number_missing")
        return
    current = str(out.get("invoice_number") or "").strip()
    if current and not str(out.get("invoice_number_generated") or "").lower() == "true":
        return
    cfg = (rules.get("categories") or {}).get(category) or {}
    if cfg.get("invoice_number_rule") == "account_number_service_period":
        account = str(out.get("account_number") or "").strip()
        month = _service_period_start_month3(out)
        year = _service_period_end_year2(out)
        if account and month and year:
            out["invoice_number"] = f"{account} {month} {year}"
            out["invoice_number_policy_applied"] = True
            _remove_issue(out, "invoice_number_missing")


def _apply_gl_rules(out: dict[str, Any], category: str, rules: dict[str, Any]) -> None:
    cfg = (rules.get("categories") or {}).get(category) or {}
    defaults = cfg.get("default_gl_candidates") or {}
    default_gl = str(defaults.get("default") or "").strip()
    for item in out.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("gl_account_candidate") or "").strip()
        valid = ai_mapping_review.validate_gl_account(raw)
        if valid:
            item["gl_account_candidate"] = valid["gl_code"]
            item["gl_name"] = valid["gl_name"]
            continue

        desc_key = ai_mapping_review.normalize_key(str(item.get("description") or ""))
        matched_default = ""
        if category == "trash_collection_services":
            if any(k in desc_key for k in ("fuel recovery", "environmental", "trash", "waste", "container", "dumpster", "service")):
                matched_default = str(defaults.get("fuel_recovery") or default_gl)
        elif category == "utilities":
            for keyword, gl_code in defaults.items():
                if keyword != "default" and keyword in desc_key:
                    matched_default = str(gl_code)
                    break
        matched_default = matched_default or default_gl
        valid = ai_mapping_review.validate_gl_account(matched_default)
        if valid:
            item["gl_account_candidate"] = valid["gl_code"]
            item["gl_name"] = valid["gl_name"]
            item["gl_suggestion_source"] = "canonical_category_default"
            continue

        try:
            candidates = ai_mapping_review.gl_candidates(
                line_item_description=str(item.get("description") or ""),
                vendor_name=str(out.get("vendor_name") or ""),
                ai_suggested_gl=raw,
                limit=4,
            ).get("candidates") or []
        except Exception:
            candidates = []
        item["gl_candidates"] = candidates
        top = candidates[0] if candidates else None
        if top and top.get("valid") and float(top.get("score") or 0) >= 0.88:
            item["gl_account_candidate"] = top.get("gl_code") or top.get("gl_account") or ""
            item["gl_name"] = top.get("gl_name") or ""
            item["gl_suggestion_source"] = "canonical_candidate_engine"


def _apply_canonical_descriptions(out: dict[str, Any], category: str, rules: dict[str, Any]) -> None:
    invoice_format = (
        rules.get("template_columns", {})
        .get("invoice_description", {})
        .get("formats", {})
        .get(category)
    ) or rules.get("template_columns", {}).get("invoice_description", {}).get("formats", {}).get("unknown")
    line_format = (
        rules.get("template_columns", {})
        .get("line_item_description", {})
        .get("formats", {})
        .get(category)
    ) or rules.get("template_columns", {}).get("line_item_description", {}).get("formats", {}).get("unknown")

    context = _format_context(out)
    sample_item = next((item for item in out.get("line_items") or [] if isinstance(item, dict)), {})
    invoice_description = _render_format(invoice_format, out, sample_item, context)
    if invoice_description:
        out["canonical_invoice_description"] = invoice_description[:180]

    for item in out.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        rendered = _render_format(line_format, out, item, context)
        if rendered:
            item["canonical_line_item_description"] = rendered[:240]


def _apply_required_field_validation(out: dict[str, Any], rules: dict[str, Any]) -> None:
    blocking: list[str] = []
    required = rules.get("template_requirements", {}).get("required_columns") or REQUIRED_TEMPLATE_COLUMNS
    field_values = {
        "Invoice Number": out.get("invoice_number"),
        "Bill or Credit": out.get("bill_or_credit") or "Bill",
        "Invoice Date": out.get("invoice_date"),
        "Accounting Date": out.get("invoice_date"),
        "Vendor": out.get("vendor_name"),
        "Invoice Description": out.get("canonical_invoice_description") or out.get("invoice_description"),
        "Due Date": out.get("due_date"),
    }
    for header, value in field_values.items():
        if header in required and not _clean(value):
            blocking.append(header)

    items = [item for item in out.get("line_items") or [] if isinstance(item, dict)]
    if not items and "Line Item Description" in required:
        blocking.append("Line Items")
    for item in items:
        row_values = {
            "Property Abbreviation": out.get("property_abbreviation"),
            "GL Account": item.get("gl_account_candidate"),
            "Line Item Description": item.get("canonical_line_item_description") or item.get("description"),
            "Amount": item.get("amount"),
            "Expense Type": item.get("expense_type") or "General",
            "Is Replacement Reserve": False if item.get("is_replacement_reserve") is None else item.get("is_replacement_reserve"),
            "Line Item Number": 1,
        }
        for header, value in row_values.items():
            if header in required and value in ("", None):
                blocking.append(header)

    seen: set[str] = set()
    out["blocking_required_fields"] = [x for x in blocking if not (x in seen or seen.add(x))]
    for header in out["blocking_required_fields"]:
        code = _required_code(header)
        _add_issue(out, code, f"{header} is required by Canonical Rules before this invoice can be exported.", "high")


def _render_format(fmt: str, normalized: dict[str, Any], item: dict[str, Any], context: dict[str, str]) -> str:
    if not fmt:
        return ""
    values = {
        **context,
        "vendor_name": _display_vendor(normalized),
        "invoice_date_short": _short_date(normalized.get("invoice_date")),
        "line_item_description": _clean(item.get("description")) or "Invoice total",
        "line_item_description_short": _concise_item(_clean(item.get("description"))),
        "main_item_or_category": _concise_item(_clean(item.get("description"))) or _clean(normalized.get("category_label")),
        "amount": _format_amount(item.get("amount")),
    }
    rendered = fmt
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    rendered = re.sub(r"\s+", " ", rendered).strip()
    rendered = re.sub(r"\s+-\s+-\s+", " - ", rendered)
    rendered = rendered.strip(" -")
    return proper_case_preserve_acronyms(rendered)


def _format_context(normalized: dict[str, Any]) -> dict[str, str]:
    period = _service_period_label(normalized)
    prop_or_site = _property_or_site(normalized)
    service_address = normalize_service_address_for_description(normalized.get("service_address"))
    service_address_or_property = service_address or prop_or_site
    return {
        "service_period": period,
        "property_or_site_name": prop_or_site,
        "service_address_or_property": service_address_or_property,
        "property_abbreviation": _clean(normalized.get("property_abbreviation")),
        "service_address": service_address,
    }


def _property_or_site(normalized: dict[str, Any]) -> str:
    candidate = _clean(normalized.get("property_candidate"))
    if candidate and candidate.lower() != _clean(normalized.get("property_abbreviation")).lower():
        return candidate
    match = normalized.get("property_match") or {}
    for key in ("property_name", "Property Name"):
        value = _clean(match.get(key)) if isinstance(match, dict) else ""
        if value:
            return value
    address = _clean(normalized.get("service_address"))
    if address:
        return _first_address_phrase(address)
    return _clean(normalized.get("property_abbreviation"))


def _service_period_label(normalized: dict[str, Any]) -> str:
    start = _short_date(normalized.get("service_period_start"))
    end = _short_date(normalized.get("service_period_end"))
    return f"{start}-{end}" if start and end else ""


def _service_period_start_month3(normalized: dict[str, Any]) -> str:
    value = _parse_date(normalized.get("service_period_start") or normalized.get("invoice_date"))
    return value.strftime("%b") if value else ""


def _service_period_end_year2(normalized: dict[str, Any]) -> str:
    value = _parse_date(normalized.get("service_period_end") or normalized.get("invoice_date"))
    return value.strftime("%y") if value else ""


def _short_date(value: Any) -> str:
    parsed = _parse_date(value)
    return parsed.strftime("%m/%d/%y") if parsed else _clean(value)


def _parse_date(value: Any) -> datetime | None:
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _format_amount(value: Any) -> str:
    try:
        return f"{float(value or 0):.2f}"
    except (TypeError, ValueError):
        return ""


def _display_vendor(normalized: dict[str, Any]) -> str:
    return _clean(normalized.get("vendor_name") or normalized.get("raw_vendor_name"))


def _concise_item(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if not text:
        return ""
    generic = {"invoice total", "miscellaneous", "maintenance supplies", "hardware and miscellaneous items"}
    if text.lower() in generic:
        return ""
    words = text.split()
    return " ".join(words[:8])[:72]


def _first_address_phrase(value: str) -> str:
    return value.split(",", 1)[0].strip()


def _category_allows_blank_location(category: str, rules: dict[str, Any]) -> bool:
    policy = ((rules.get("categories") or {}).get(category) or {}).get("location_policy")
    return policy == "property_level_blank_location_allowed"


def _looks_like_raw_address(value: str) -> bool:
    text = value.strip()
    return bool(re.search(r"\d+\s+\w+", text) and ("," in text or re.search(r"\b(st|street|ave|road|rd|dr|ct|ln)\b", text, re.I)))


def _required_code(header: str) -> str:
    return "required_" + re.sub(r"[^a-z0-9]+", "_", header.lower()).strip("_")


def _add_issue(out: dict[str, Any], code: str, message: str, severity: str = "medium") -> None:
    issues = list(out.get("manual_review_issues") or [])
    if not any(issue.get("code") == code for issue in issues if isinstance(issue, dict)):
        issues.append({"code": code, "message": message, "severity": severity})
    out["manual_review_issues"] = issues
    codes = list(out.get("manual_review_codes") or [])
    if code not in codes:
        codes.append(code)
    out["manual_review_codes"] = codes
    reasons = list(out.get("manual_review_reasons") or [])
    if message not in reasons:
        reasons.append(message)
    out["manual_review_reasons"] = reasons


def _remove_issue(out: dict[str, Any], code: str) -> None:
    removed_messages = {
        i.get("message")
        for i in out.get("manual_review_issues") or []
        if isinstance(i, dict) and i.get("code") == code and i.get("message")
    }
    out["manual_review_issues"] = [i for i in out.get("manual_review_issues") or [] if not isinstance(i, dict) or i.get("code") != code]
    out["manual_review_codes"] = [c for c in out.get("manual_review_codes") or [] if c != code]
    out["manual_review_reasons"] = [r for r in out.get("manual_review_reasons") or [] if r not in removed_messages]


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _column_key(label: str) -> str:
    cleaned = _clean(label).lower()
    direct = HEADER_TO_COLUMN_KEY.get(cleaned)
    if direct:
        return direct
    return re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")


def _category_key(label: str) -> str:
    text = ai_mapping_review.normalize_key(label)
    if not text:
        return ""
    if "util" in text or "internet telecom" in text:
        return "utilities"
    if "pest" in text:
        return "pest_control"
    if "landscap" in text:
        return "landscaping"
    if "marketing" in text:
        return "marketing"
    if "subscription" in text or "suscription" in text:
        return "subscriptions"
    if "trash" in text or "waste" in text or "garbage" in text:
        return "trash_collection_services"
    if "maintenance" in text or "supplies" in text or "purchase" in text or "unfrequent" in text or "infrequent" in text:
        return "other_infrequent"
    if text == "unknown":
        return "unknown"
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _category_label(category: str, rules: dict[str, Any]) -> str:
    labels = ((rules.get("categories") or {}).get(category) or {}).get("labels") or []
    return str(labels[0]) if labels else category.replace("_", " ").title()


def _vendor_yaml_category(vendor_name: str) -> str:
    key = ai_mapping_review.mapping_key(vendor_name)
    path = settings.VENDORS_DIR / f"{key}.yaml"
    if not path.is_file():
        return ""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return ""
    identity = data.get("vendor_identity") or {}
    return str(identity.get("category") or "")


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


__all__ = [
    "CANONICAL_RULES_XLSX",
    "CANONICAL_RULES_YAML",
    "REQUIRED_TEMPLATE_COLUMNS",
    "canonicalize_normalized_invoice",
    "classify_invoice_category",
    "default_rules",
    "import_canonical_rules_from_excel",
    "load_rules",
    "prompt_rules_summary",
    "required_columns",
    "reset_cache",
]
