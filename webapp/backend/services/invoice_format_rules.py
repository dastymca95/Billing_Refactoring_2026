"""Configurable ResMan invoice/output formatting rules.

These rules are intentionally separate from vendor extraction logic. They only
control how validated invoice data is rendered into required ResMan fields such
as Number, Invoice Description, and Line Item Description.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .. import settings


CONFIG_PATH = settings.PROJECT_ROOT / "config" / "invoice_format_rules.yaml"
BACKUP_DIR = settings.PROJECT_ROOT / "config" / ".backups" / "invoice_format_rules"


DEFAULT_RULES: dict[str, Any] = {
    "version": 1,
    "updated_at": "",
    "description": "Configurable output-format rules for AI-assisted invoices and bills.",
    "rule_priority": [
        "vendor",
        "vendor_group",
        "property",
        "property_group",
        "gl_account",
        "gl_group",
        "general",
    ],
    "groups": {
        "vendor_groups": {
            "utilities": {
                "label": "Utilities",
                "vendors": [
                    "City of Chattanooga Wastewater Department",
                    "Pennyrile Electric",
                    "Hopkinsville Water",
                    "McMinnville Electric",
                ],
            },
            "variable_suppliers": {
                "label": "Variable suppliers",
                "vendors": ["HD Supply", "Lowe's Pro Supply", "Home Depot"],
            },
        },
        "gl_groups": {
            "utilities": {
                "label": "Utilities",
                "gl_accounts": ["6930", "6935", "6955"],
            },
            "repairs": {
                "label": "Repairs and maintenance",
                "gl_accounts": ["6470", "6530", "6615", "6651", "6675"],
            },
        },
        "property_groups": {},
    },
    "template_requirements": {
        "required_columns": [
            "Bill or Credit",
            "Invoice Number",
            "Invoice Date",
            "Vendor",
            "Invoice Description",
            "Line Item Number",
            "Property Abbreviation",
            "GL Account",
            "Amount",
            "Expense Type",
            "Is Replacement Reserve",
        ],
    },
    "rules": [
        {
            "id": "general_bill_default",
            "name": "General bill default",
            "enabled": True,
            "priority": 10,
            "scope": {"type": "general", "value": ""},
            "document_type": "bill",
            "templates": {
                "invoice_number": "BILL-{account_number}-{invoice_date_yyyymmdd}",
                "invoice_description": "{service_period_range} - {service_address_or_property} - {line_item_description_short}",
                "line_item_description": "{service_period_range} - {service_address_or_property} - {line_item_description}",
            },
        },
        {
            "id": "general_invoice_default",
            "name": "General invoice default",
            "enabled": True,
            "priority": 1,
            "scope": {"type": "general", "value": ""},
            "document_type": "invoice",
            "templates": {
                "invoice_number": "BILL-{account_number}-{invoice_date_yyyymmdd}",
                "invoice_description": "{invoice_date_short} - {vendor_name} - {property_abbreviation} - {line_item_description_short}",
                "line_item_description": "{line_item_description}",
            },
        },
    ],
}


VARIABLES: list[dict[str, str]] = [
    {"key": "account_number", "label": "Account number"},
    {"key": "invoice_date_short", "label": "Invoice date MM/DD/YY"},
    {"key": "invoice_date_yyyymmdd", "label": "Invoice date YYYYMMDD"},
    {"key": "service_period_range", "label": "03/26/26-04/27/26"},
    {"key": "service_period_start_month3", "label": "Mar"},
    {"key": "service_period_start_month3_upper", "label": "MAR"},
    {"key": "service_period_end_year2", "label": "26"},
    {"key": "vendor_name", "label": "Vendor"},
    {"key": "property_abbreviation", "label": "Property"},
    {"key": "service_address", "label": "Service address"},
    {"key": "service_address_or_property", "label": "Address fallback property"},
    {"key": "gl_account", "label": "GL code"},
    {"key": "gl_name", "label": "GL name"},
    {"key": "line_item_description", "label": "Line item"},
    {"key": "line_item_description_short", "label": "Short line item"},
    {"key": "amount", "label": "Amount"},
    {"key": "source_file_stem", "label": "Source file name"},
]


PRESETS: dict[str, list[dict[str, str]]] = {
    "invoice_number": [
        {
            "label": "Bill account + bill date",
            "template": "BILL-{account_number}-{invoice_date_yyyymmdd}",
            "description": "Current safe default for bills without a formal invoice number.",
        },
        {
            "label": "Account + service month/year",
            "template": "{account_number}-{service_period_start_month3_upper}{service_period_end_year2}",
            "description": "Example: 040582701-01-MAR26.",
        },
        {
            "label": "Vendor + account + period",
            "template": "{vendor_key}-{account_number}-{service_period_start_month3_upper}{service_period_end_year2}",
            "description": "Useful when account numbers repeat across vendors.",
        },
    ],
    "invoice_description": [
        {
            "label": "Service period + address + line",
            "template": "{service_period_range} - {service_address_or_property} - {line_item_description_short}",
            "description": "Best for recurring bills and utilities.",
        },
        {
            "label": "Date + vendor + property + line",
            "template": "{invoice_date_short} - {vendor_name} - {property_abbreviation} - {line_item_description_short}",
            "description": "Good for supplier invoices.",
        },
    ],
    "line_item_description": [
        {
            "label": "Service period + address + full line",
            "template": "{service_period_range} - {service_address_or_property} - {line_item_description}",
            "description": "Best for manager-readable bill line items.",
        },
        {
            "label": "Full source line item",
            "template": "{line_item_description}",
            "description": "Keeps the AI/source line item exactly as normalized.",
        },
    ],
}


class InvoiceFormatRulesError(ValueError):
    pass


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        return deepcopy(DEFAULT_RULES)
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # pragma: no cover - defensive config recovery
        raise InvoiceFormatRulesError(f"Could not read invoice format rules: {exc}") from exc
    return normalize_config(data)


def save_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_config(config)
    normalized["updated_at"] = datetime.now().isoformat(timespec="seconds")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"invoice_format_rules_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"
        backup.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = CONFIG_PATH.with_suffix(f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(
        yaml.safe_dump(normalized, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    tmp.replace(CONFIG_PATH)
    try:
        from .template_rules import reset_cache

        reset_cache()
    except Exception:
        pass
    return normalized


def normalize_config(data: dict[str, Any]) -> dict[str, Any]:
    cfg = deepcopy(DEFAULT_RULES)
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "groups" and isinstance(value, dict):
                groups = deepcopy(cfg["groups"])
                for group_key, group_value in value.items():
                    if isinstance(group_value, dict):
                        groups[group_key] = group_value
                cfg["groups"] = groups
            elif key == "rules":
                cfg["rules"] = _normalize_rules(value)
            else:
                cfg[key] = value
    cfg["rules"] = _normalize_rules(cfg.get("rules"))
    cfg["template_requirements"] = _normalize_template_requirements(
        cfg.get("template_requirements")
    )
    cfg.setdefault("groups", deepcopy(DEFAULT_RULES["groups"]))
    cfg.setdefault("rule_priority", deepcopy(DEFAULT_RULES["rule_priority"]))
    return cfg


def _normalize_template_requirements(value: Any) -> dict[str, list[str]]:
    default = deepcopy(DEFAULT_RULES["template_requirements"])
    if not isinstance(value, dict):
        return default
    required = value.get("required_columns")
    if not isinstance(required, list):
        return default
    clean_required: list[str] = []
    seen: set[str] = set()
    for item in required:
        text = _clean(item)
        if not text or text in seen:
            continue
        seen.add(text)
        clean_required.append(text)
    return {"required_columns": clean_required}


def _normalize_rules(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        value = DEFAULT_RULES["rules"]
    out: list[dict[str, Any]] = []
    for idx, raw in enumerate(value):
        if not isinstance(raw, dict):
            continue
        scope = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}
        templates = raw.get("templates") if isinstance(raw.get("templates"), dict) else {}
        rule = {
            "id": _clean(raw.get("id")) or f"rule_{idx + 1}",
            "name": _clean(raw.get("name")) or "Format rule",
            "enabled": bool(raw.get("enabled", True)),
            "priority": _int(raw.get("priority"), 0),
            "scope": {
                "type": _clean(scope.get("type")) or "general",
                "value": _clean(scope.get("value")),
            },
            "document_type": (_clean(raw.get("document_type")) or "any").lower(),
            "templates": {
                "invoice_number": _clean(templates.get("invoice_number")),
                "invoice_description": _clean(templates.get("invoice_description")),
                "line_item_description": _clean(templates.get("line_item_description")),
            },
        }
        if rule["document_type"] not in {"any", "bill", "invoice"}:
            rule["document_type"] = "any"
        if rule["scope"]["type"] not in {
            "general",
            "vendor",
            "vendor_group",
            "gl_account",
            "gl_group",
            "property",
            "property_group",
        }:
            rule["scope"]["type"] = "general"
            rule["scope"]["value"] = ""
        out.append(rule)
    return out or deepcopy(DEFAULT_RULES["rules"])


def generate_required_invoice_number(
    payload: dict[str, Any],
    *,
    invoice_date: str,
    total_amount: float,
    service_period_start: str = "",
    service_period_end: str = "",
) -> str:
    context = build_context(
        {
            "vendor_name": payload.get("vendor_name"),
            "raw_vendor_name": payload.get("vendor_name"),
            "invoice_date": invoice_date,
            "account_number": payload.get("account_number"),
            "service_address": payload.get("service_address"),
            "property_abbreviation": payload.get("property_abbreviation"),
            "property_candidate": payload.get("property_candidate"),
            "service_period_start": service_period_start,
            "service_period_end": service_period_end,
            "bill_or_credit": payload.get("bill_or_credit") or "Bill",
        },
        {},
        source_file=_clean(payload.get("_source_file")),
        total_amount=total_amount,
    )
    rendered = render_field("invoice_number", context)
    if _meaningful_invoice_number(rendered):
        return _sanitize_invoice_number(rendered)

    account = context.get("account_number")
    if account:
        return _sanitize_invoice_number(f"BILL-{account}-{context['invoice_date_yyyymmdd']}")
    seed = "|".join(
        [
            context.get("vendor_name", ""),
            context.get("service_address", ""),
            context.get("invoice_date_yyyymmdd", ""),
            f"{float(total_amount or 0):.2f}",
            context.get("source_file_stem", ""),
        ]
    )
    digest = hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:8].upper()
    return _sanitize_invoice_number(f"BILL-{context['invoice_date_yyyymmdd']}-{digest}")


def render_invoice_number(
    normalized: dict[str, Any],
    item: dict[str, Any] | None = None,
    *,
    fallback: str = "",
    source_file: str = "",
    total_amount: float | None = None,
) -> str:
    """Render the final ResMan invoice number from Formats.

    Provider-extracted invoice numbers remain available as provenance, but the
    operator-facing `Formats` policy is the canonical source for the value that
    lands in the ResMan template.
    """
    context = build_context(
        normalized,
        item or {},
        source_file=source_file,
        total_amount=total_amount,
    )
    rendered = render_field("invoice_number", context)
    if _meaningful_invoice_number(rendered):
        return _sanitize_invoice_number(rendered)
    if _meaningful_invoice_number(fallback):
        return _sanitize_invoice_number(fallback)
    return ""


def render_invoice_description(
    normalized: dict[str, Any],
    item: dict[str, Any],
    *,
    fallback: str = "",
    source_file: str = "",
) -> str:
    context = build_context(normalized, item, source_file=source_file)
    rendered = render_field("invoice_description", context)
    return rendered[:180] if rendered else fallback[:180]


def render_line_item_description(
    normalized: dict[str, Any],
    item: dict[str, Any],
    *,
    fallback: str = "",
    source_file: str = "",
) -> str:
    context = build_context(normalized, item, source_file=source_file)
    rendered = render_field("line_item_description", context)
    return rendered[:240] if rendered else fallback[:240]


def render_field(field: str, context: dict[str, str]) -> str:
    rule = effective_rule(context, field)
    template = ((rule or {}).get("templates") or {}).get(field, "")
    return render_template(template, context)


def preview(config: dict[str, Any] | None = None, sample: dict[str, Any] | None = None) -> dict[str, str]:
    cfg = normalize_config(config or load_config())
    context = _sample_context(sample or {})
    invoice_number = render_template(
        _template_for_config(cfg, "invoice_number", context),
        context,
    )
    return {
        "invoice_number": _sanitize_invoice_number(invoice_number),
        "invoice_description": render_template(
            _template_for_config(cfg, "invoice_description", context),
            context,
        ),
        "line_item_description": render_template(
            _template_for_config(cfg, "line_item_description", context),
            context,
        ),
    }


def effective_rule(context: dict[str, str], field: str) -> dict[str, Any] | None:
    return _effective_rule_from_config(load_config(), context, field)


def _template_for_config(cfg: dict[str, Any], field: str, context: dict[str, str]) -> str:
    rule = _effective_rule_from_config(cfg, context, field)
    return ((rule or {}).get("templates") or {}).get(field, "")


def _effective_rule_from_config(
    cfg: dict[str, Any],
    context: dict[str, str],
    field: str,
) -> dict[str, Any] | None:
    document_type = context.get("document_type", "invoice")
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for rule in cfg.get("rules") or []:
        if not rule.get("enabled", True):
            continue
        template = ((rule.get("templates") or {}).get(field) or "").strip()
        if not template:
            continue
        rule_doc = (rule.get("document_type") or "any").lower()
        if rule_doc not in {"any", document_type}:
            continue
        match_score = _scope_match_score(rule, cfg, context)
        if match_score < 0:
            continue
        candidates.append((_int(rule.get("priority"), 0), match_score, rule))
    if not candidates:
        return None
    candidates.sort(key=lambda part: (part[0], part[1]), reverse=True)
    return candidates[0][2]


def _scope_match_score(rule: dict[str, Any], cfg: dict[str, Any], context: dict[str, str]) -> int:
    scope = rule.get("scope") or {}
    scope_type = scope.get("type") or "general"
    value = _clean(scope.get("value"))
    if scope_type == "general":
        return 1
    if scope_type == "vendor":
        needles = {_normalize(value)}
        hay = {_normalize(context.get("vendor_name", "")), _normalize(context.get("raw_vendor_name", ""))}
        return 90 if needles & hay else -1
    if scope_type == "vendor_group":
        group = ((cfg.get("groups") or {}).get("vendor_groups") or {}).get(value) or {}
        vendors = {_normalize(v) for v in group.get("vendors") or []}
        hay = {_normalize(context.get("vendor_name", "")), _normalize(context.get("raw_vendor_name", ""))}
        return 70 if vendors & hay else -1
    if scope_type == "property":
        return 80 if value and value.lower() == context.get("property_abbreviation", "").lower() else -1
    if scope_type == "property_group":
        group = ((cfg.get("groups") or {}).get("property_groups") or {}).get(value) or {}
        props = {str(p).lower() for p in group.get("properties") or []}
        return 60 if context.get("property_abbreviation", "").lower() in props else -1
    if scope_type == "gl_account":
        return 75 if value and value == context.get("gl_account", "") else -1
    if scope_type == "gl_group":
        group = ((cfg.get("groups") or {}).get("gl_groups") or {}).get(value) or {}
        gls = {str(gl) for gl in group.get("gl_accounts") or []}
        return 55 if context.get("gl_account", "") in gls else -1
    return -1


def build_context(
    normalized: dict[str, Any],
    item: dict[str, Any],
    *,
    source_file: str = "",
    total_amount: float | None = None,
) -> dict[str, str]:
    invoice_date = _normalize_date(_clean(normalized.get("invoice_date")))
    service_start = _normalize_date(_clean(normalized.get("service_period_start")))
    service_end = _normalize_date(_clean(normalized.get("service_period_end")))
    line_desc = _clean(item.get("description")) or "Invoice total"
    gl_name = _clean(item.get("gl_name"))
    amount = item.get("amount") if item else total_amount
    service_address = _clean(normalized.get("service_address"))
    property_abbr = _clean(normalized.get("property_abbreviation"))
    document_type = "bill" if (_clean(normalized.get("bill_or_credit")).lower() or "bill") == "bill" else "invoice"
    return {
        "document_type": document_type,
        "vendor_name": _clean(normalized.get("vendor_name") or normalized.get("raw_vendor_name")),
        "raw_vendor_name": _clean(normalized.get("raw_vendor_name") or normalized.get("vendor_name")),
        "vendor_key": _normalize(_clean(normalized.get("vendor_name") or normalized.get("raw_vendor_name"))).replace(" ", "_"),
        "account_number": _clean(normalized.get("account_number")),
        "invoice_date_short": _short_date(invoice_date),
        "invoice_date_yyyymmdd": _date_token(invoice_date),
        "service_period_start": service_start,
        "service_period_end": service_end,
        "service_period_range": _service_period_range(service_start, service_end),
        "service_period_start_month3": _month3(service_start, upper=False),
        "service_period_start_month3_upper": _month3(service_start, upper=True),
        "service_period_end_year2": _year2(service_end),
        "service_address": service_address,
        "service_address_or_property": service_address or property_abbr or _clean(normalized.get("property_candidate")),
        "property_abbreviation": property_abbr,
        "property_candidate": _clean(normalized.get("property_candidate")),
        "gl_account": _clean(item.get("gl_account_candidate") or item.get("gl_account")),
        "gl_name": gl_name,
        "line_item_description": line_desc,
        "line_item_description_short": _concise(line_desc),
        "amount": _format_amount(amount),
        "source_file_stem": Path(source_file or _clean(normalized.get("source_file"))).stem,
    }


def render_template(template: str, context: dict[str, str]) -> str:
    if not template:
        return ""

    def repl(match: re.Match[str]) -> str:
        return str(context.get(match.group(1), ""))

    rendered = re.sub(r"\{([A-Za-z0-9_]+)\}", repl, template)
    rendered = re.sub(r"\s*-\s*(?:-\s*)+", " - ", rendered)
    rendered = re.sub(r"\s+", " ", rendered).strip(" -")
    return rendered.strip()


def references_payload(references: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    vendors = sorted(
        [
            {
                "vendor_name": _clean(v.get("vendor_name")),
                "vendor_id": _clean(v.get("vendor_id")),
                "status": _clean(v.get("status")),
                "default_gl": _clean(v.get("default_gl")),
            }
            for v in references.get("vendors", [])
            if _clean(v.get("vendor_name"))
        ],
        key=lambda v: v["vendor_name"].lower(),
    )
    gl_accounts = sorted(
        [
            {
                "gl_code": _clean(g.get("gl_code")),
                "gl_name": _clean(g.get("gl_description") or g.get("chart_of_accounts_description")),
                "type": _clean(g.get("gl_account_type")),
            }
            for g in references.get("gl_accounts", [])
            if _clean(g.get("gl_code"))
        ],
        key=lambda g: (g["gl_code"], g["gl_name"].lower()),
    )
    property_map: dict[str, dict[str, str]] = {}
    for row in references.get("properties", []):
        abbr = _clean(row.get("Property Abbreviation"))
        if not abbr:
            continue
        property_map.setdefault(
            abbr,
            {
                "property_abbreviation": abbr,
                "property_name": _clean(row.get("Property Name")),
            },
        )
    properties = sorted(property_map.values(), key=lambda p: p["property_abbreviation"].lower())
    return {"vendors": vendors, "gl_accounts": gl_accounts, "properties": properties}


def _sample_context(sample: dict[str, Any]) -> dict[str, str]:
    normalized = {
        "vendor_name": sample.get("vendor_name") or "City of Chattanooga Wastewater Department",
        "raw_vendor_name": sample.get("vendor_name") or "City of Chattanooga Wastewater Department",
        "bill_or_credit": sample.get("bill_or_credit") or "Bill",
        "account_number": sample.get("account_number") or "040582701-01",
        "invoice_date": sample.get("invoice_date") or "05/11/2026",
        "service_period_start": sample.get("service_period_start") or "03/26/2026",
        "service_period_end": sample.get("service_period_end") or "04/27/2026",
        "service_address": sample.get("service_address") or "1400 N Chamberlain AVE",
        "property_abbreviation": sample.get("property_abbreviation") or "TFF",
    }
    item = {
        "description": sample.get("line_item_description") or "Rate 1 minimum up to 2,054 gals",
        "gl_account_candidate": sample.get("gl_account") or "6955",
        "gl_name": sample.get("gl_name") or "Water & Sewer",
        "amount": sample.get("amount") or 52.97,
    }
    return build_context(normalized, item, source_file="sample_bill.pdf")


def _meaningful_invoice_number(value: str) -> bool:
    core = re.sub(r"[^A-Za-z0-9]+", "", value or "").lower()
    return len(core.replace("bill", "")) >= 4


def _sanitize_invoice_number(value: str) -> str:
    clean = _clean(value)
    # Formats is the operator-facing source of truth, so preserve intentional
    # separators such as spaces, hyphens, slashes, and dots. Only remove
    # control characters that cannot safely live in a spreadsheet cell.
    clean = re.sub(r"[\x00-\x1f\x7f]+", "", clean)
    return clean.strip(" ._-")[:40]


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower().replace("&", " and ")).strip()


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_date(value: str) -> str:
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return datetime.strptime(value, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return ""


def _short_date(value: str) -> str:
    if not value:
        return ""
    return datetime.strptime(value, "%m/%d/%Y").strftime("%m/%d/%y")


def _date_token(value: str) -> str:
    if not value:
        return datetime.now().strftime("%Y%m%d")
    return datetime.strptime(value, "%m/%d/%Y").strftime("%Y%m%d")


def _service_period_range(start: str, end: str) -> str:
    if not start or not end:
        return ""
    return f"{_short_date(start)}-{_short_date(end)}"


def _month3(value: str, *, upper: bool) -> str:
    if not value:
        return ""
    label = datetime.strptime(value, "%m/%d/%Y").strftime("%b")
    return label.upper() if upper else label


def _year2(value: str) -> str:
    if not value:
        return ""
    return datetime.strptime(value, "%m/%d/%Y").strftime("%y")


def _concise(value: str) -> str:
    words = _clean(value).split()
    if len(words) > 10:
        return " ".join(words[:10])
    return " ".join(words)


def _format_amount(value: Any) -> str:
    try:
        return f"{float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "0.00"
