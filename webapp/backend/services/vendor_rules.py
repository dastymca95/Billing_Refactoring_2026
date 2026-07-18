"""Vendor Rules Studio — load/edit/save vendor YAML rule files.

Phase 1Z. The CLI processors at
    Training Bills_Invoices/Water - Sewer/<vendor>/process_<vendor>.py
already read their behaviour from `config/vendors/<vendor_key>.yaml` via
`yaml.safe_load`. This module is the *editing* counterpart:

  * `list_editable_vendors()`         — vendors the UI is allowed to edit.
  * `load_vendor_rules(vendor_key)`   — read the YAML from disk (no cache:
                                        we want the editor to reflect the
                                        current file every time it opens).
  * `editable_groups(vendor_key)`     — UI-friendly grouping/labelling of
                                        the editable subset.
  * `validate_patch(vendor_key, patch)` — cheap schema-level validation.
  * `apply_patch(vendor_key, patch)`  — validate, back up, atomic-write.

Safety:
  * vendor_key must match a known YAML file name (no path traversal).
  * Only whitelisted YAML sections are writeable; unknown / non-whitelisted
    keys in the patch are rejected.
  * Unknown YAML fields outside the whitelist are *preserved* untouched.
  * Every successful save creates `config/vendors/backups/<vendor_key>_<utc>.yaml`
    before overwriting.
  * Atomic write: temp file in same directory, then `Path.replace()`.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..settings import PROJECT_ROOT
from . import deterministic_coverage


VENDORS_DIR = PROJECT_ROOT / "config" / "vendors"
BACKUPS_DIR = VENDORS_DIR / "backups"


# ----------------------------------------------------------------------------
# Vendor whitelist + display metadata
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# Editable section whitelist
# ----------------------------------------------------------------------------
# Keep this conservative. Anything not listed here is shown in the UI as
# read-only (or hidden). The patch endpoint will reject any field whose
# dotted path doesn't start with one of these prefixes.
EDITABLE_PREFIXES: tuple[str, ...] = (
    "vendor_identity.vendor_name",
    "vendor_identity.aliases",
    "vendor_identity.active",
    "vendor_identity.detection_keywords",
    "input_files.accepted_file_types",
    "invoice_number_rules.format",
    "invoice_number_rules.month_case",
    "invoice_number_rules.year_format",
    "invoice_number_rules.final_bill_suffix",
    "invoice_date_rules.fallback_strategy",
    "due_date_rules.fallback_strategy",
    "due_date_rules.fallback_offset_days",
    "service_period_rules.output_format",
    "service_period_rules.vendor_default_fallback.enabled",
    "service_period_rules.vendor_default_fallback.start_offset_months",
    "service_period_rules.vendor_default_fallback.end_offset_months",
    "service_period_rules.vendor_default_fallback.day_of_month",
    "service_period_rules.late_notice_handling",
    "service_gl_mapping",  # whole map editable; we validate cell types
    "amount_rules.tolerance",
    "amount_rules.rounding_decimals",
    "support_document_rules.enabled",
    "support_document_rules.pdf_multi_bill_handling.enabled",
    "manual_review_triggers",  # whole map of bool flags
)

# Read-only display sections — surfaced in the UI but cannot be patched.
READ_ONLY_GROUPS: tuple[str, ...] = (
    "pdf_extraction_rules",
    "property_address_overrides",
    "account_property_unit_mapping",
    "disconnection_notice_extraction_rules",
    "change_log",
)


# ----------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------


class VendorRulesError(ValueError):
    """Raised for any user-facing rule-error so the API can return 400."""


# ----------------------------------------------------------------------------
# Vendor key + path safety
# ----------------------------------------------------------------------------


_VENDOR_KEY_RE = re.compile(r"^[a-z0-9_]+$")


def _check_vendor_key(vendor_key: str) -> str:
    if not vendor_key or not _VENDOR_KEY_RE.match(vendor_key):
        raise VendorRulesError("Invalid vendor key.")
    coverage = deterministic_coverage.coverage_for_key(vendor_key)
    if coverage is None:
        raise VendorRulesError(
            f"Vendor '{vendor_key}' has no registered deterministic processor."
        )
    if not coverage.config_present:
        raise VendorRulesError(
            f"Vendor '{vendor_key}' is code-managed and has no editable declarative configuration."
        )
    return vendor_key


def _vendor_yaml_path(vendor_key: str) -> Path:
    """Resolve and traversal-check the YAML path."""
    coverage = deterministic_coverage.coverage_for_key(vendor_key)
    configured = deterministic_coverage.config_path_for_key(
        vendor_key, coverage.processor_module if coverage else "",
    )
    candidate = configured.resolve() if configured else (VENDORS_DIR / f"{vendor_key}.yaml").resolve()
    base = VENDORS_DIR.resolve()
    try:
        candidate.relative_to(base)
    except ValueError as e:
        raise VendorRulesError("Invalid vendor path.") from e
    if not candidate.is_file():
        raise VendorRulesError(f"Vendor YAML not found: {vendor_key}.yaml")
    return candidate


# ----------------------------------------------------------------------------
# Public API: list / load
# ----------------------------------------------------------------------------


def list_editable_vendors() -> list[dict[str, Any]]:
    """Return UI-shaped list of vendors the studio can edit."""
    out: list[dict[str, Any]] = []
    for coverage in deterministic_coverage.inventory():
        if not coverage.config_present:
            continue
        path = deterministic_coverage.config_path_for_key(
            coverage.vendor_key, coverage.processor_module,
        )
        status = coverage.status
        last_updated: str | None = None
        category = "Deterministic processor"
        if path and path.is_file():
            try:
                rules = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                rules = {}
                status = "yaml_error"
            identity = (rules or {}).get("vendor_identity") or {}
            category = str(identity.get("category") or category)
            if not identity.get("active", True):
                status = "inactive"
            mtime = _dt.datetime.fromtimestamp(
                path.stat().st_mtime, tz=_dt.timezone.utc
            )
            last_updated = mtime.isoformat()
        out.append(
            {
                "vendor_key": coverage.vendor_key,
                "display_name": coverage.display_name,
                "category": category,
                "status": status,
                "last_updated": last_updated,
                "editable": coverage.editable,
                "implementation_kind": coverage.implementation_kind,
            }
        )
    return out


def load_vendor_rules(vendor_key: str) -> dict[str, Any]:
    """Load the raw YAML dict for a vendor (no cache)."""
    _check_vendor_key(vendor_key)
    path = _vendor_yaml_path(vendor_key)
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise VendorRulesError(f"YAML parse error: {e}") from e
    if not isinstance(data, dict):
        raise VendorRulesError("Vendor YAML must be a mapping at the top level.")
    return data


# ----------------------------------------------------------------------------
# Editable schema — UI-friendly grouping
# ----------------------------------------------------------------------------


def _get_dotted(data: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _field(
    label: str,
    path: str,
    type_: str,
    *,
    description: str = "",
    example: str = "",
    options: list[str] | None = None,
    placeholder: str = "",
) -> dict[str, Any]:
    f: dict[str, Any] = {
        "label": label,
        "path": path,
        "type": type_,
        "editable": _is_editable_path(path),
    }
    if description:
        f["description"] = description
    if example:
        f["example"] = example
    if options:
        f["options"] = options
    if placeholder:
        f["placeholder"] = placeholder
    return f


def editable_groups(vendor_key: str) -> list[dict[str, Any]]:
    """Build the UI-shaped group list with current values baked in.

    Each group has a label, a description, a list of fields, and may carry
    a `read_only_summary` block for risky sections we surface but don't
    let the operator edit yet.
    """
    rules = load_vendor_rules(vendor_key)

    groups: list[dict[str, Any]] = []

    coverage = deterministic_coverage.coverage_for_key(vendor_key)
    pattern_fields = [
        _field(
            item.label,
            item.path,
            "string_list",
            description="Declarative matching evidence. Changes are validated, backed up and auditable.",
        )
        for item in (coverage.patterns if coverage else [])
        if item.path not in {"vendor_identity.aliases", "vendor_identity.detection_keywords"}
    ]
    groups.append({
        "key": "deterministic_patterns",
        "label": "Deterministic matching patterns",
        "description": "Safe declarative patterns used by this registered processor. Python logic remains read-only.",
        "fields": pattern_fields,
    })

    # 1. Vendor Identity ------------------------------------------------------
    groups.append(
        {
            "key": "vendor_identity",
            "label": "Vendor Identity",
            "description": "How this vendor is shown in the UI and matched on incoming files.",
            "fields": [
                _field("Display name", "vendor_identity.vendor_name", "string"),
                _field(
                    "Active",
                    "vendor_identity.active",
                    "boolean",
                    description="When off, the vendor still parses but is hidden from default workflows.",
                ),
                _field(
                    "Aliases",
                    "vendor_identity.aliases",
                    "string_list",
                    description="Alternative names operators may use.",
                ),
                _field(
                    "Detection keywords",
                    "vendor_identity.detection_keywords",
                    "string_list",
                    description="Phrases that signal a file belongs to this vendor.",
                ),
                _field(
                    "Accepted file types",
                    "input_files.accepted_file_types",
                    "string_list",
                    description="File extensions, lowercase, no dots (e.g. pdf, csv).",
                    example="pdf, csv, xlsx",
                ),
            ],
        }
    )

    # 2. Invoice Number Rules -------------------------------------------------
    inv_format = _get_dotted(rules, "invoice_number_rules.format", "")
    groups.append(
        {
            "key": "invoice_number_rules",
            "label": "Invoice Number",
            "description": "How the canonical invoice number is built.",
            "fields": [
                _field(
                    "Format",
                    "invoice_number_rules.format",
                    "string",
                    description="Template using {account_number}, {service_month_abbrev_title}, {service_year_yy}.",
                    example=str(inv_format) or "{account_number} {service_month_abbrev_title} {service_year_yy}",
                ),
                _field(
                    "Month case",
                    "invoice_number_rules.month_case",
                    "enum",
                    options=["upper", "title", "lower"],
                ),
                _field(
                    "Year format",
                    "invoice_number_rules.year_format",
                    "enum",
                    options=["yy", "yyyy"],
                ),
                _field(
                    "Final bill suffix",
                    "invoice_number_rules.final_bill_suffix",
                    "string",
                    description="Text appended when the bill is the final/closeout bill.",
                ),
            ],
        }
    )

    # 3. Date Rules -----------------------------------------------------------
    groups.append(
        {
            "key": "date_rules",
            "label": "Dates",
            "description": "Invoice date and due date sourcing.",
            "fields": [
                _field(
                    "Invoice date fallback",
                    "invoice_date_rules.fallback_strategy",
                    "enum",
                    options=["last_day_to_pay_minus_15_days", "manual_review", "none"],
                ),
                _field(
                    "Due date fallback",
                    "due_date_rules.fallback_strategy",
                    "enum",
                    options=["invoice_date_plus_days", "manual_review", "none"],
                ),
                _field(
                    "Due date offset (days)",
                    "due_date_rules.fallback_offset_days",
                    "integer",
                    description="Used only when the fallback strategy is 'invoice_date_plus_days'.",
                ),
            ],
        }
    )

    # 4. Service Period -------------------------------------------------------
    groups.append(
        {
            "key": "service_period_rules",
            "label": "Service Period",
            "description": "How the start/end of service are determined when the bill doesn't show explicit dates.",
            "fields": [
                _field(
                    "Output format",
                    "service_period_rules.output_format",
                    "string",
                    example="MM/DD/YY-MM/DD/YY",
                ),
                _field(
                    "Vendor default fallback enabled",
                    "service_period_rules.vendor_default_fallback.enabled",
                    "boolean",
                    description="When on, we infer the service period from the invoice date if the bill doesn't show one.",
                ),
                _field(
                    "Start offset (months)",
                    "service_period_rules.vendor_default_fallback.start_offset_months",
                    "integer",
                    description="Negative = months before invoice date. Example: -1 = previous month.",
                ),
                _field(
                    "End offset (months)",
                    "service_period_rules.vendor_default_fallback.end_offset_months",
                    "integer",
                ),
                _field(
                    "Day of month",
                    "service_period_rules.vendor_default_fallback.day_of_month",
                    "integer",
                    description="1-31. Used for both start and end.",
                ),
                _field(
                    "Late-notice handling",
                    "service_period_rules.late_notice_handling",
                    "enum",
                    options=["use_last_day_to_pay_minus_15_days", "manual_review", "none"],
                ),
            ],
        }
    )

    # 5. GL Mapping -----------------------------------------------------------
    gl_map = _get_dotted(rules, "service_gl_mapping", {}) or {}
    gl_fields: list[dict[str, Any]] = []
    if isinstance(gl_map, dict):
        for service_key in sorted(gl_map.keys()):
            entry = gl_map[service_key]
            gl_code_path = f"service_gl_mapping.{service_key}.gl_code"
            gl_fields.append(
                _field(
                    f"{service_key.replace('_', ' ').title()} → GL code",
                    gl_code_path,
                    "string",
                    description=str(
                        (entry or {}).get("description") or ""
                    ) if isinstance(entry, dict) else "",
                )
            )
    groups.append(
        {
            "key": "service_gl_mapping",
            "label": "GL Account Mapping",
            "description": "How service groups become ResMan GL codes (water/sewer, gas, sanitation, etc.).",
            "fields": gl_fields,
        }
    )

    # 6. Total reconciliation -------------------------------------------------
    groups.append(
        {
            "key": "amount_rules",
            "label": "Total Reconciliation",
            "description": "How tightly we require line-items to add up to the bill total.",
            "fields": [
                _field(
                    "Tolerance ($)",
                    "amount_rules.tolerance",
                    "number",
                    description="Maximum allowed difference between extracted total and the sum of line items.",
                    example="0.02",
                ),
                _field(
                    "Rounding decimals",
                    "amount_rules.rounding_decimals",
                    "integer",
                ),
            ],
        }
    )

    # 7. Support documents / Dropbox -----------------------------------------
    groups.append(
        {
            "key": "support_document_rules",
            "label": "Support Documents",
            "description": "Per-invoice document links written into the export.",
            "fields": [
                _field(
                    "Upload enabled",
                    "support_document_rules.enabled",
                    "boolean",
                ),
                _field(
                    "Split multi-bill PDFs",
                    "support_document_rules.pdf_multi_bill_handling.enabled",
                    "boolean",
                ),
            ],
        }
    )

    # 8. Manual review triggers ----------------------------------------------
    triggers = _get_dotted(rules, "manual_review_triggers", {}) or {}
    if not isinstance(triggers, dict):
        triggers = {}
    trigger_fields = [
        _field(
            _humanize(name),
            f"manual_review_triggers.{name}",
            "boolean",
            description=_trigger_description(name),
        )
        for name in sorted(triggers.keys())
    ]
    groups.append(
        {
            "key": "manual_review_triggers",
            "label": "Manual Review Triggers",
            "description": "Conditions that send an invoice to manual review instead of auto-export.",
            "fields": trigger_fields,
        }
    )

    # 9. Read-only sections ---------------------------------------------------
    for ro_key in READ_ONLY_GROUPS:
        if ro_key in rules:
            groups.append(
                {
                    "key": ro_key,
                    "label": _humanize(ro_key),
                    "description": "Currently controlled by processor code or operator-managed reference data. Not editable yet.",
                    "fields": [],
                    "read_only_summary": _summarize_section(rules[ro_key]),
                }
            )

    # Bake current values into each field for the UI to pre-populate.
    for group in groups:
        for f in group["fields"]:
            f["value"] = _get_dotted(rules, f["path"])
    return groups


def _humanize(key: str) -> str:
    return key.replace("_", " ").replace(".", " · ").title()


def _trigger_description(name: str) -> str:
    # A few high-signal triggers worth describing; otherwise return "".
    lookup = {
        "missing_service_period": "Flag invoices where no explicit service period or reading dates are found.",
        "service_period_inferred": "Flag invoices where the service period had to be inferred from the invoice date.",
        "unit_mapping_not_found": "Flag invoices whose account number can't be matched to a known unit.",
        "property_abbreviation_missing": "Flag invoices with no property abbreviation after all fallbacks.",
        "late_notice_detected": "Flag bills detected as late or disconnect notices.",
        "amount_total_mismatch": "Flag invoices where the sum of line items doesn't match the bill total within tolerance.",
    }
    return lookup.get(name, "")


def _summarize_section(value: Any) -> dict[str, Any]:
    """Return a compact summary suitable for read-only display."""
    if isinstance(value, list):
        return {"kind": "list", "count": len(value)}
    if isinstance(value, dict):
        return {"kind": "object", "keys": sorted(list(value.keys()))[:20]}
    return {"kind": "scalar", "preview": str(value)[:200]}


# ----------------------------------------------------------------------------
# Patch validation + save
# ----------------------------------------------------------------------------


def _is_editable_path(dotted: str) -> bool:
    if any(dotted == p or dotted.startswith(p + ".") for p in EDITABLE_PREFIXES):
        return True
    leaf = dotted.rsplit(".", 1)[-1].casefold()
    return any(token in leaf for token in ("pattern", "keyword", "alias", "contains"))


def _set_dotted(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur: Any = data
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


_GL_CODE_RE = re.compile(r"^\d{3,6}$")
_FILE_TYPE_RE = re.compile(r"^[a-z0-9]{1,8}$")


def validate_patch(vendor_key: str, patch: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a list of validation issues. Empty list = OK."""
    _check_vendor_key(vendor_key)
    rules = load_vendor_rules(vendor_key)
    issues: list[dict[str, Any]] = []

    if not isinstance(patch, dict) or not patch:
        return [{"path": "", "message": "Patch must be a non-empty mapping."}]

    for dotted, value in _flatten(patch).items():
        if not _is_editable_path(dotted):
            issues.append(
                {
                    "path": dotted,
                    "message": f"This field is not editable in the studio: {dotted}.",
                }
            )
            continue

        if not any(dotted == p or dotted.startswith(p + ".") for p in EDITABLE_PREFIXES):
            current = _get_dotted(rules, dotted, default=None)
            if not isinstance(current, list) or not all(isinstance(item, str) for item in current):
                issues.append({
                    "path": dotted,
                    "message": "Only existing declarative string-list patterns may be edited.",
                })
                continue

        # Per-field type/value validation.
        try:
            _validate_value(dotted, value)
        except VendorRulesError as e:
            issues.append({"path": dotted, "message": str(e)})

    return issues


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts to dotted-path keys; lists/scalars are leaves."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def _validate_value(dotted: str, value: Any) -> None:
    # Vendor identity
    if dotted == "vendor_identity.vendor_name":
        if not isinstance(value, str) or not value.strip():
            raise VendorRulesError("Display name cannot be empty.")
        return
    if dotted == "vendor_identity.active":
        if not isinstance(value, bool):
            raise VendorRulesError("'Active' must be true or false.")
        return
    if dotted in ("vendor_identity.aliases", "vendor_identity.detection_keywords"):
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise VendorRulesError("Must be a list of strings.")
        return

    leaf = dotted.rsplit(".", 1)[-1].casefold()
    if any(token in leaf for token in ("pattern", "keyword", "alias", "contains")):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise VendorRulesError("Pattern values must be a list of strings.")
        if any(len(item) > 1000 for item in value) or len(value) > 500:
            raise VendorRulesError("Pattern list exceeds the safe size limit.")
        return

    # File types
    if dotted == "input_files.accepted_file_types":
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise VendorRulesError("Must be a list of file-type strings.")
        for s in value:
            if not _FILE_TYPE_RE.match(s):
                raise VendorRulesError(
                    f"'{s}' is not a valid file type. Use lowercase, no dot (e.g. pdf, csv)."
                )
        return

    # Invoice number rules
    if dotted == "invoice_number_rules.format":
        if not isinstance(value, str) or not value.strip():
            raise VendorRulesError("Format cannot be empty.")
        # detect unbalanced braces
        if value.count("{") != value.count("}"):
            raise VendorRulesError("Unbalanced { } in invoice format.")
        return
    if dotted == "invoice_number_rules.month_case":
        if value not in {"upper", "title", "lower"}:
            raise VendorRulesError("Month case must be 'upper', 'title', or 'lower'.")
        return
    if dotted == "invoice_number_rules.year_format":
        if value not in {"yy", "yyyy"}:
            raise VendorRulesError("Year format must be 'yy' or 'yyyy'.")
        return
    if dotted == "invoice_number_rules.final_bill_suffix":
        if value is not None and not isinstance(value, str):
            raise VendorRulesError("Final bill suffix must be text or empty.")
        return

    # Dates
    if dotted == "due_date_rules.fallback_offset_days":
        if not isinstance(value, int) or value < 0 or value > 90:
            raise VendorRulesError("Offset days must be between 0 and 90.")
        return
    if dotted in (
        "invoice_date_rules.fallback_strategy",
        "due_date_rules.fallback_strategy",
        "service_period_rules.late_notice_handling",
    ):
        if not isinstance(value, str) or not value:
            raise VendorRulesError("Strategy cannot be empty.")
        return

    # Service period
    if dotted == "service_period_rules.output_format":
        if not isinstance(value, str) or not value.strip():
            raise VendorRulesError("Output format cannot be empty.")
        return
    if dotted.startswith("service_period_rules.vendor_default_fallback."):
        leaf = dotted.rsplit(".", 1)[1]
        if leaf == "enabled":
            if not isinstance(value, bool):
                raise VendorRulesError("Must be true or false.")
            return
        if leaf in ("start_offset_months", "end_offset_months"):
            if not isinstance(value, int) or value < -36 or value > 36:
                raise VendorRulesError("Offset months must be between -36 and 36.")
            return
        if leaf == "day_of_month":
            if not isinstance(value, int) or not (1 <= value <= 31):
                raise VendorRulesError("Day of month must be 1-31.")
            return

    # GL mapping
    if dotted.startswith("service_gl_mapping.") and dotted.endswith(".gl_code"):
        if not isinstance(value, str) or not _GL_CODE_RE.match(value):
            raise VendorRulesError("GL account must be a 3-6 digit ResMan GL code.")
        return

    # Amounts
    if dotted == "amount_rules.tolerance":
        if not isinstance(value, (int, float)) or value < 0 or value > 100:
            raise VendorRulesError("Tolerance must be between 0 and 100.")
        return
    if dotted == "amount_rules.rounding_decimals":
        if not isinstance(value, int) or not (0 <= value <= 6):
            raise VendorRulesError("Rounding decimals must be between 0 and 6.")
        return

    # Support docs
    if dotted in (
        "support_document_rules.enabled",
        "support_document_rules.pdf_multi_bill_handling.enabled",
    ):
        if not isinstance(value, bool):
            raise VendorRulesError("Must be true or false.")
        return

    # Manual review triggers
    if dotted.startswith("manual_review_triggers."):
        if not isinstance(value, bool):
            raise VendorRulesError("Trigger must be true or false.")
        return

    # Catch-all — accept (already passed editability check)
    return


def apply_patch(vendor_key: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Validate, back up, atomic-write. Returns {backup_path, written_paths}."""
    _check_vendor_key(vendor_key)
    issues = validate_patch(vendor_key, patch)
    if issues:
        raise VendorRulesError(_format_issues(issues))

    yaml_path = _vendor_yaml_path(vendor_key)
    rules = load_vendor_rules(vendor_key)

    # Apply patch into the loaded dict.
    flat = _flatten(patch)
    for dotted, value in flat.items():
        _set_dotted(rules, dotted, value)

    # Backup before write.
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUPS_DIR / f"{vendor_key}_{ts}.yaml"
    backup_path.write_bytes(yaml_path.read_bytes())

    # Serialize. Try to keep YAML readable: block style, no aliases, sorted=False.
    new_text = yaml.safe_dump(
        rules,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
    )

    # Atomic write: temp file in same directory, then replace.
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{vendor_key}_", suffix=".yaml.tmp", dir=str(yaml_path.parent)
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_text)
        tmp_path.replace(yaml_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    deterministic_coverage.invalidate_inventory_cache()

    return {
        "vendor_key": vendor_key,
        "written_paths": sorted(flat.keys()),
        "backup_filename": backup_path.name,
    }


def _format_issues(issues: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    for it in issues:
        p = it.get("path") or "(root)"
        m = it.get("message") or "Invalid value."
        parts.append(f"{p}: {m}")
    return " | ".join(parts)


def restore_latest_backup(vendor_key: str) -> dict[str, Any]:
    """Restore the most recent backup for this vendor. Returns metadata."""
    _check_vendor_key(vendor_key)
    yaml_path = _vendor_yaml_path(vendor_key)
    if not BACKUPS_DIR.is_dir():
        raise VendorRulesError("No backups available.")
    candidates = sorted(
        [p for p in BACKUPS_DIR.glob(f"{vendor_key}_*.yaml") if p.is_file()],
        reverse=True,
    )
    if not candidates:
        raise VendorRulesError("No backups available for this vendor.")
    latest = candidates[0]
    yaml_path.write_bytes(latest.read_bytes())
    deterministic_coverage.invalidate_inventory_cache()
    return {"vendor_key": vendor_key, "restored_from": latest.name}
