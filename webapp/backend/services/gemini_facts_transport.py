"""Gemini-only transport contract for observable invoice facts.

This module deliberately stops at provider transport and local normalization.
It cannot select GL, decide readiness, authorize export, or promote learning.
Raw provider content is accepted only through a deterministic JSON parser and
is never included in safe diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError


TRANSPORT_SCHEMA_VERSION = "gemini-facts-transport/1.0"
TRANSPORT_PROMPT_VERSION = "gemini-facts-only/2.0"


ObservedNumber = str | int | float | Decimal | None


class GeminiTransportModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class GeminiEvidenceTransport(GeminiTransportModel):
    page: int | None
    text: str | None
    bbox: list[float] | None
    source_type: str | None
    confidence: float | None


class GeminiLineItemTransport(GeminiTransportModel):
    source_page: int | None
    section_header: str | None
    row_label: str | None
    location_candidate: str | None
    activity: str | None
    raw_description: str | None
    quantity: ObservedNumber
    unit_price: ObservedNumber
    amount: ObservedNumber
    tax: ObservedNumber
    confidence: float | None
    evidence: list[GeminiEvidenceTransport]


class GeminiPaidMarkerTransport(GeminiTransportModel):
    page: int | None
    text: str | None
    bbox: list[float] | None
    confidence: float | None


class GeminiAmountComponentTransport(GeminiTransportModel):
    label: str | None
    amount: ObservedNumber


class GeminiExcludedPaidRowTransport(GeminiTransportModel):
    raw_apartment_number: str | None
    component_amounts: list[GeminiAmountComponentTransport]
    row_total: ObservedNumber
    paid_marker_evidence: list[GeminiPaidMarkerTransport]
    exclusion_reason: str | None


class GeminiUnresolvedRegionTransport(GeminiTransportModel):
    page: int | None
    field: str | None
    bbox: list[float] | None
    reason: str | None
    confidence: float | None


class GeminiPageReconciliationTransport(GeminiTransportModel):
    page: int | None
    component_total: ObservedNumber
    printed_total: ObservedNumber
    status: str | None


class GeminiFactsTransport(GeminiTransportModel):
    vendor_name: str | None
    invoice_number: str | None
    invoice_date: str | None
    service_date: str | None
    due_date: str | None
    due_date_text: str | None
    payment_terms: str | None
    bill_or_credit: str | None
    account_number: str | None
    service_address: str | None
    sold_to_raw_text: str | None
    job_site_raw_text: str | None
    address_role: str | None
    location_candidate: str | None
    service_period_start: str | None
    service_period_end: str | None
    service_period: str | None
    property_candidate: str | None
    property_abbreviation: str | None
    invoice_description: str | None
    line_items: list[GeminiLineItemTransport]
    excluded_paid_rows: list[GeminiExcludedPaidRowTransport]
    subtotal: ObservedNumber
    tax_amount: ObservedNumber
    shipping_amount: ObservedNumber
    fees_amount: ObservedNumber
    total_amount: ObservedNumber
    visual_extraction_status: str | None
    unresolved_visual_regions: list[GeminiUnresolvedRegionTransport]
    page_reconciliations: list[GeminiPageReconciliationTransport]
    evidence: list[GeminiEvidenceTransport]
    warnings: list[str]
    confidence: float | None


class GeminiTransportError(ValueError):
    """Base fail-closed transport error carrying only safe diagnostics."""

    failure_code = "gemini_transport_invalid"

    def __init__(self, message: str, diagnostic: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.diagnostic = dict(diagnostic)


class GeminiTransportJSONError(GeminiTransportError):
    failure_code = "gemini_transport_invalid_json"


class GeminiTransportSchemaError(GeminiTransportError):
    failure_code = "gemini_transport_invalid_schema"


class SafeSchemaFailureCategory(str, Enum):
    RAW_JSON_PARSER_FAILURE = "raw_json_parser_failure"
    TRUNCATION = "truncation"
    OUTPUT_LIMIT_EXHAUSTION = "output_limit_exhaustion"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INCORRECT_FIELD_TYPE = "incorrect_field_type"
    ADDITIONAL_UNSUPPORTED_FIELD = "additional_unsupported_field"
    MULTIPLE_JSON_OBJECTS = "multiple_json_objects"
    TRANSPORT_SCHEMA_VALIDATION_FAILURE = "transport_schema_validation_failure"
    INTERNAL_NORMALIZATION_FAILURE = "internal_normalization_failure"
    UNCLASSIFIED = "unclassified"


_REQUIRED_FIELDS = frozenset(GeminiFactsTransport.model_fields)
_KNOWN_FIELDS = _REQUIRED_FIELDS
_OUTER_FENCE = re.compile(
    r"\A```(?:json)?[ \t]*\r?\n(?P<body>.*)\r?\n```[ \t]*\Z",
    re.IGNORECASE | re.DOTALL,
)
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%B %d, %Y",
    "%b %d, %Y",
)


def gemini_facts_transport_json_schema() -> dict[str, Any]:
    """Return the small Gemini-compatible JSON schema used on the wire."""

    nullable_string = {"type": ["string", "null"]}
    nullable_number = {"type": ["string", "number", "null"]}
    nullable_integer = {"type": ["integer", "null"]}
    nullable_confidence = {"type": ["number", "null"]}
    nullable_bbox = {
        "type": ["array", "null"],
        "items": {"type": "number"},
    }
    evidence = {
        "type": "object",
        "properties": {
            "page": nullable_integer,
            "text": nullable_string,
            "bbox": nullable_bbox,
            "source_type": nullable_string,
            "confidence": nullable_confidence,
        },
        "required": ["page", "text", "bbox", "source_type", "confidence"],
    }
    line = {
        "type": "object",
        "properties": {
            "source_page": nullable_integer,
            "section_header": nullable_string,
            "row_label": nullable_string,
            "location_candidate": nullable_string,
            "activity": nullable_string,
            "raw_description": nullable_string,
            "quantity": nullable_number,
            "unit_price": nullable_number,
            "amount": nullable_number,
            "tax": nullable_number,
            "confidence": nullable_confidence,
            "evidence": {"type": "array", "items": evidence},
        },
        "required": [
            "source_page", "section_header", "row_label", "location_candidate",
            "activity", "raw_description", "quantity", "unit_price", "amount",
            "tax", "confidence", "evidence",
        ],
    }
    paid_marker = {
        "type": "object",
        "properties": {
            "page": nullable_integer,
            "text": nullable_string,
            "bbox": nullable_bbox,
            "confidence": nullable_confidence,
        },
        "required": ["page", "text", "bbox", "confidence"],
    }
    amount_component = {
        "type": "object",
        "properties": {"label": nullable_string, "amount": nullable_number},
        "required": ["label", "amount"],
    }
    excluded_paid_row = {
        "type": "object",
        "properties": {
            "raw_apartment_number": nullable_string,
            "component_amounts": {"type": "array", "items": amount_component},
            "row_total": nullable_number,
            "paid_marker_evidence": {"type": "array", "items": paid_marker},
            "exclusion_reason": nullable_string,
        },
        "required": [
            "raw_apartment_number", "component_amounts", "row_total",
            "paid_marker_evidence", "exclusion_reason",
        ],
    }
    unresolved = {
        "type": "object",
        "properties": {
            "page": nullable_integer,
            "field": nullable_string,
            "bbox": nullable_bbox,
            "reason": nullable_string,
            "confidence": nullable_confidence,
        },
        "required": ["page", "field", "bbox", "reason", "confidence"],
    }
    reconciliation = {
        "type": "object",
        "properties": {
            "page": nullable_integer,
            "component_total": nullable_number,
            "printed_total": nullable_number,
            "status": nullable_string,
        },
        "required": ["page", "component_total", "printed_total", "status"],
    }
    properties: dict[str, Any] = {
        key: nullable_string for key in (
            "vendor_name", "invoice_number", "invoice_date", "service_date",
            "due_date", "due_date_text", "payment_terms", "bill_or_credit",
            "account_number", "service_address", "sold_to_raw_text",
            "job_site_raw_text", "address_role", "location_candidate",
            "service_period_start", "service_period_end", "service_period",
            "property_candidate", "property_abbreviation", "invoice_description",
            "visual_extraction_status",
        )
    }
    properties.update({
        "line_items": {"type": "array", "items": line},
        "excluded_paid_rows": {"type": "array", "items": excluded_paid_row},
        "subtotal": nullable_number,
        "tax_amount": nullable_number,
        "shipping_amount": nullable_number,
        "fees_amount": nullable_number,
        "total_amount": nullable_number,
        "unresolved_visual_regions": {"type": "array", "items": unresolved},
        "page_reconciliations": {"type": "array", "items": reconciliation},
        "evidence": {"type": "array", "items": evidence},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "confidence": nullable_confidence,
    })
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
    }


def gemini_response_format() -> dict[str, Any]:
    """OpenAI-compatible Gemini structured-output declaration."""

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "innerview_observed_document_facts",
            "strict": True,
            "schema": gemini_facts_transport_json_schema(),
        },
    }


def build_gemini_facts_prompt() -> str:
    return "\n".join((
        "Extract only facts visible in the supplied document images.",
        "Use the response schema. Return JSON only, without Markdown or prose.",
        "Use null for unknown or illegible values; never guess missing text.",
        "Preserve visible row order and raw wording. Include page and bounding-box evidence when available.",
        "Keep printed dates distinct. due_date_text may contain terms such as Upon Receipt.",
        "Put visibly PAID or crossed-out rows only in excluded_paid_rows with their visible amounts and marker evidence.",
        "Do not collapse a visible table into an invoice-total line.",
        "Do not return GL accounts, accounting policy, readiness, export, tenant, labels, corrections, or learning decisions.",
        "Do not provide rationale or chain-of-thought.",
    ))


def parse_and_normalize_gemini_facts(
    raw_response: str,
    *,
    provider: str,
    model: str,
    request_profile: str,
    response_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(response_metadata or {})
    if bool(metadata.get("output_token_limit_reached")):
        diagnostic = build_safe_diagnostic(
            raw_response,
            provider=provider,
            model=model,
            request_profile=request_profile,
            response_metadata=metadata,
            parser_error_type="OutputTokenLimitReached",
        )
        raise GeminiTransportJSONError(
            "Gemini structured response reached the configured output limit.", diagnostic
        )
    try:
        parsed = extract_single_json_object(raw_response)
    except _DeterministicJSONError as exc:
        diagnostic = build_safe_diagnostic(
            raw_response,
            provider=provider,
            model=model,
            request_profile=request_profile,
            response_metadata=metadata,
            parser_error_type=exc.error_type,
            parser_error_offset=exc.offset,
        )
        raise GeminiTransportJSONError(
            "Gemini structured response was not deterministic valid JSON.", diagnostic
        ) from exc
    try:
        transport = GeminiFactsTransport.model_validate(parsed)
    except ValidationError as exc:
        first = (exc.errors(include_url=False) or [{}])[0]
        diagnostic = build_safe_diagnostic(
            raw_response,
            provider=provider,
            model=model,
            request_profile=request_profile,
            response_metadata=metadata,
            parsed=parsed,
            parser_error_type="SchemaValidationError",
            schema_validation_error_path=_safe_error_path(first.get("loc") or ()),
            schema_validation_error_type=str(first.get("type") or ""),
        )
        raise GeminiTransportSchemaError(
            "Gemini transport response did not match the facts schema.", diagnostic
        ) from exc
    return normalize_transport(transport)


class _DeterministicJSONError(ValueError):
    def __init__(self, error_type: str, offset: int | None = None) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.offset = offset


def extract_single_json_object(raw_response: str) -> dict[str, Any]:
    """Extract one complete object without repair, guessing, or object ranking."""

    if not isinstance(raw_response, str):
        raise _DeterministicJSONError("ResponseNotText")
    trimmed = raw_response.strip()
    if not trimmed:
        raise _DeterministicJSONError("EmptyResponse", 0)
    fence = _OUTER_FENCE.fullmatch(trimmed)
    if fence:
        trimmed = fence.group("body").strip()
    elif trimmed.startswith("```") or trimmed.endswith("```"):
        raise _DeterministicJSONError("InvalidMarkdownFence", 0)
    try:
        value = json.loads(trimmed)
    except json.JSONDecodeError as direct_error:
        object_start = trimmed.find("{")
        if object_start < 0:
            raise _DeterministicJSONError(
                _json_error_type(trimmed, direct_error), direct_error.pos
            ) from direct_error
        decoder = json.JSONDecoder()
        try:
            value, consumed = decoder.raw_decode(trimmed[object_start:])
        except json.JSONDecodeError as exc:
            raise _DeterministicJSONError(
                _json_error_type(trimmed, exc), object_start + exc.pos
            ) from exc
        prefix = trimmed[:object_start]
        suffix = trimmed[object_start + consumed:]
        if not _harmless_prose(prefix, allow_empty=True):
            raise _DeterministicJSONError("CompetingOrStructuredPrefix", 0)
        if not _harmless_prose(suffix, allow_empty=True):
            raise _DeterministicJSONError(
                "TrailingStructuredData", object_start + consumed
            )
    if not isinstance(value, dict):
        raise _DeterministicJSONError("TopLevelNotObject", 0)
    return value


def normalize_transport(transport: GeminiFactsTransport) -> dict[str, Any]:
    unknown_paths = sorted(_unknown_field_paths(transport))
    warning_values = {
        *(_blank_to_none(item) for item in transport.warnings),
        *(f"gemini_transport_unknown_field:{path}" for path in unknown_paths),
    } - {None}

    def number(value: ObservedNumber, path: str) -> Decimal | None:
        normalized = _decimal_or_none(value)
        if value is not None and str(value).strip() and normalized is None:
            warning_values.add(f"gemini_transport_invalid_numeric:{path}")
        return normalized
    dates: list[dict[str, Any]] = []
    for field in ("invoice_date", "service_date", "due_date"):
        raw_value = _blank_to_none(getattr(transport, field))
        dates.append({
            "field": field,
            "raw_value": raw_value,
            "normalized_candidate": _date_candidate(raw_value),
            "provenance": "document_observed" if raw_value else "unresolved",
        })

    def evidence_rows(values: list[GeminiEvidenceTransport]) -> list[dict[str, Any]]:
        rows = [{
            "page": item.page,
            "text": _blank_to_none(item.text),
            "normalized_text": None,
            "bbox": list(item.bbox) if item.bbox is not None else None,
            "source_type": _blank_to_none(item.source_type) or "document_observation",
            "extraction_method": "gemini_facts_transport",
            "confidence": item.confidence,
        } for item in values]
        return sorted(rows, key=lambda row: (
            row.get("page") is None, row.get("page") or 0,
            tuple(row.get("bbox") or ()), row.get("source_type") or "",
        ))

    line_items = []
    for line_index, item in enumerate(transport.line_items):
        raw_description = _blank_to_none(item.raw_description)
        line_items.append({
            "source_page": item.source_page,
            "section_header": _blank_to_none(item.section_header),
            "row_label": _blank_to_none(item.row_label),
            "location_candidate": _blank_to_none(item.location_candidate),
            "activity": _blank_to_none(item.activity),
            "description": raw_description,
            "raw_description": raw_description,
            "normalized_description": None,
            "generated_description": None,
            "quantity": number(item.quantity, f"line_items.{line_index}.quantity"),
            "unit_price": number(item.unit_price, f"line_items.{line_index}.unit_price"),
            "amount": number(item.amount, f"line_items.{line_index}.amount"),
            "tax": number(item.tax, f"line_items.{line_index}.tax"),
            "confidence": item.confidence,
            "evidence": evidence_rows(item.evidence),
        })

    excluded_paid_rows = [{
        "raw_apartment_number": _blank_to_none(item.raw_apartment_number),
        "component_amounts": [{
            "label": _blank_to_none(component.label),
            "amount": number(
                component.amount,
                f"excluded_paid_rows.{row_index}.component_amounts.{component_index}.amount",
            ),
        } for component_index, component in enumerate(item.component_amounts)],
        "row_total": number(item.row_total, f"excluded_paid_rows.{row_index}.row_total"),
        "paid_marker_evidence": [{
            "page": marker.page,
            "text": _blank_to_none(marker.text),
            "bbox": list(marker.bbox) if marker.bbox is not None else None,
            "confidence": marker.confidence,
        } for marker in item.paid_marker_evidence],
        "exclusion_reason": _blank_to_none(item.exclusion_reason),
    } for row_index, item in enumerate(transport.excluded_paid_rows)]

    scalar_strings = (
        "vendor_name", "invoice_number", "invoice_date", "service_date", "due_date",
        "due_date_text", "payment_terms", "bill_or_credit", "account_number",
        "service_address", "sold_to_raw_text", "job_site_raw_text", "address_role",
        "location_candidate", "service_period_start", "service_period_end",
        "service_period", "property_candidate", "property_abbreviation",
        "invoice_description", "visual_extraction_status",
    )
    normalized = {field: _blank_to_none(getattr(transport, field)) for field in scalar_strings}
    normalized.update({
        "line_items": line_items,
        "excluded_paid_rows": excluded_paid_rows,
        "subtotal": number(transport.subtotal, "subtotal"),
        "tax_amount": number(transport.tax_amount, "tax_amount"),
        "shipping_amount": number(transport.shipping_amount, "shipping_amount"),
        "fees_amount": number(transport.fees_amount, "fees_amount"),
        "total_amount": number(transport.total_amount, "total_amount"),
        "confidence": transport.confidence,
        "warnings": sorted(warning_values),
        "needs_manual_review": bool(warning_values or transport.unresolved_visual_regions),
        "unresolved_visual_regions": [
            item.model_dump(mode="python", exclude_none=False)
            for item in transport.unresolved_visual_regions
        ],
        "page_reconciliations": [{
            "page": item.page,
            "component_total": number(
                item.component_total, f"page_reconciliations.{index}.component_total",
            ),
            "printed_total": number(
                item.printed_total, f"page_reconciliations.{index}.printed_total",
            ),
            "status": _blank_to_none(item.status),
        } for index, item in enumerate(transport.page_reconciliations)],
        "evidence": evidence_rows(transport.evidence),
        "observed_date_candidates": dates,
        "transport_schema_version": TRANSPORT_SCHEMA_VERSION,
        "transport_prompt_version": TRANSPORT_PROMPT_VERSION,
    })
    return normalized


def build_safe_diagnostic(
    raw_response: str,
    *,
    provider: str,
    model: str,
    request_profile: str,
    response_metadata: Mapping[str, Any] | None = None,
    parsed: Mapping[str, Any] | None = None,
    parser_error_type: str = "",
    parser_error_offset: int | None = None,
    schema_validation_error_path: str = "",
    schema_validation_error_type: str = "",
) -> dict[str, Any]:
    """Return an allow-listed response-shape diagnostic containing no values."""

    raw = raw_response if isinstance(raw_response, str) else ""
    metadata = dict(response_metadata or {})
    stripped = raw.strip()
    parsed_fields = dict(parsed or {})
    names = sorted(_safe_field_name(name) for name in parsed_fields)
    types = {
        _safe_field_name(name): _value_type(value)
        for name, value in sorted(parsed_fields.items(), key=lambda item: str(item[0]))
    }
    unknown = set(parsed_fields) - _KNOWN_FIELDS
    missing = _REQUIRED_FIELDS - set(parsed_fields)
    diagnostic = {
        "provider": str(provider or "unknown")[:80],
        "model": str(model or "unknown")[:160],
        "request_profile": str(request_profile or "unknown")[:160],
        "response_byte_length": len(raw.encode("utf-8")),
        "response_character_length": len(raw),
        "response_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "first_non_whitespace_character_class": _character_class(stripped[:1]),
        "last_non_whitespace_character_class": _character_class(stripped[-1:]),
        "markdown_code_fence_present": "```" in raw,
        "json_object_boundary_detectable": "{" in raw and "}" in raw,
        "json_array_boundary_detectable": "[" in raw and "]" in raw,
        "json_object_boundary_count": min(raw.count("{"), raw.count("}")),
        "json_array_boundary_count": min(raw.count("["), raw.count("]")),
        "finish_reason": str(metadata.get("finish_reason") or "unknown")[:80],
        "prompt_token_count": _safe_int(metadata.get("prompt_token_count")),
        "output_token_count": _safe_int(metadata.get("output_token_count")),
        "output_token_limit_reached": bool(metadata.get("output_token_limit_reached")),
        "json_parser_error_type": str(parser_error_type or "")[:120] or None,
        "json_parser_error_character_offset": (
            max(0, int(parser_error_offset)) if parser_error_offset is not None else None
        ),
        "schema_validation_error_path": str(schema_validation_error_path or "")[:240] or None,
        "schema_validation_error_type": str(schema_validation_error_type or "")[:120] or None,
        "received_top_level_field_names": names,
        "received_top_level_value_types": types,
        "missing_required_field_names": sorted(missing),
        "unexpected_field_name_hashes": sorted(
            hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:12]
            for name in unknown
        ),
        "unknown_field_count": len(unknown),
        "missing_required_field_count": len(missing),
        "transport_schema_version": TRANSPORT_SCHEMA_VERSION,
    }
    diagnostic["schema_failure_category"] = classify_safe_diagnostic(diagnostic).value
    return diagnostic


def classify_safe_diagnostic(
    diagnostic: Mapping[str, Any],
) -> SafeSchemaFailureCategory:
    """Classify only allow-listed response-shape metadata.

    The classifier never receives or reconstructs response values.  Historical
    v1 diagnostics with a generic strict-internal path therefore remain
    honestly classified at the internal-contract boundary instead of being
    assigned an invented subtype.
    """

    parser_error = str(diagnostic.get("json_parser_error_type") or "").strip()
    parser_error_folded = parser_error.casefold()
    validation_type = str(
        diagnostic.get("schema_validation_error_type") or ""
    ).strip().casefold()
    finish_reason = str(diagnostic.get("finish_reason") or "").strip().casefold()
    if bool(diagnostic.get("output_token_limit_reached")) or finish_reason in {
        "length", "max_tokens", "max_token", "max_output_tokens",
    }:
        return SafeSchemaFailureCategory.OUTPUT_LIMIT_EXHAUSTION
    if parser_error == "TruncatedJSON":
        return SafeSchemaFailureCategory.TRUNCATION
    if parser_error in {"TrailingStructuredData", "CompetingOrStructuredPrefix"}:
        return SafeSchemaFailureCategory.MULTIPLE_JSON_OBJECTS
    if parser_error == "StrictInternalContractValidationError":
        return SafeSchemaFailureCategory.INTERNAL_NORMALIZATION_FAILURE
    if int(diagnostic.get("missing_required_field_count") or 0) > 0 \
            or validation_type in {"missing", "missing_argument"}:
        return SafeSchemaFailureCategory.MISSING_REQUIRED_FIELD
    if int(diagnostic.get("unknown_field_count") or 0) > 0 \
            or validation_type == "extra_forbidden":
        return SafeSchemaFailureCategory.ADDITIONAL_UNSUPPORTED_FIELD
    if validation_type and validation_type not in {"missing", "missing_argument"}:
        return SafeSchemaFailureCategory.INCORRECT_FIELD_TYPE
    if parser_error == "SchemaValidationError":
        return SafeSchemaFailureCategory.TRANSPORT_SCHEMA_VALIDATION_FAILURE
    if parser_error_folded:
        return SafeSchemaFailureCategory.RAW_JSON_PARSER_FAILURE
    return SafeSchemaFailureCategory.UNCLASSIFIED


def _harmless_prose(value: str, *, allow_empty: bool) -> bool:
    text = value.strip()
    if not text:
        return allow_empty
    if len(text) > 512 or any(char in text for char in "{}[]`"):
        return False
    if text.casefold() in {"true", "false", "null"}:
        return False
    if text[:1] in {'"', "'"} or text[:1].isdigit() or text[:1] in "+-":
        return False
    return bool(re.fullmatch(r"[\w\s,:;.!?()\-/]+", text, re.UNICODE))


def _json_error_type(text: str, exc: json.JSONDecodeError) -> str:
    stripped = text.rstrip()
    if stripped.count("{") > stripped.count("}") or stripped.count("[") > stripped.count("]"):
        return "TruncatedJSON"
    if exc.pos >= max(0, len(stripped) - 1):
        return "TruncatedJSON"
    return type(exc).__name__


def _blank_to_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal_or_none(value: ObservedNumber) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        decimal = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return decimal if decimal.is_finite() else None


def _date_candidate(raw_value: str | None) -> str | None:
    if not raw_value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw_value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _unknown_field_paths(model: BaseModel, prefix: str = "") -> set[str]:
    result = {
        f"{prefix}{name}" for name in (model.model_extra or {})
    }
    for field_name in type(model).model_fields:
        value = getattr(model, field_name)
        child_prefix = f"{prefix}{field_name}."
        if isinstance(value, BaseModel):
            result.update(_unknown_field_paths(value, child_prefix))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, BaseModel):
                    result.update(_unknown_field_paths(item, f"{child_prefix}{index}."))
    return result


def _safe_field_name(value: Any) -> str:
    name = str(value)
    if name in _KNOWN_FIELDS:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    return f"unknown_field_sha256:{digest}"


def _safe_error_path(parts: Any) -> str:
    safe: list[str] = []
    for part in parts:
        if isinstance(part, int):
            safe.append(str(part))
        else:
            safe.append(_safe_field_name(part))
    return ".".join(safe)


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float, Decimal)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return "other"


def _character_class(value: str) -> str:
    if not value:
        return "none"
    if value == "{":
        return "object_boundary"
    if value == "}":
        return "object_boundary"
    if value in "[]":
        return "array_boundary"
    if value in "\"'":
        return "quote"
    if value.isdigit():
        return "digit"
    if value.isalpha():
        return "letter"
    if value == "`":
        return "markdown_fence"
    return "other"


def _safe_int(value: Any) -> int | None:
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "GeminiFactsTransport",
    "GeminiTransportError",
    "GeminiTransportJSONError",
    "GeminiTransportSchemaError",
    "SafeSchemaFailureCategory",
    "TRANSPORT_PROMPT_VERSION",
    "TRANSPORT_SCHEMA_VERSION",
    "build_gemini_facts_prompt",
    "build_safe_diagnostic",
    "classify_safe_diagnostic",
    "extract_single_json_object",
    "gemini_facts_transport_json_schema",
    "gemini_response_format",
    "normalize_transport",
    "parse_and_normalize_gemini_facts",
]
