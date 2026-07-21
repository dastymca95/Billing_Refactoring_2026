"""Bounded Gemini-only verification of unresolved observable document facts.

This contract is deliberately narrower than primary extraction.  It selects
targets from local validation, asks about one visual uncertainty, and merges
the resulting observation as a separately-provenanced revision.  It has no
accounting, GL, readiness, export, benchmark, learning, or rule authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .gemini_facts_transport import extract_single_json_object


SUPPLEMENTARY_SCHEMA_VERSION = "gemini-supplementary-facts/1.1"
SUPPLEMENTARY_PROMPT_VERSION = "gemini-targeted-verification/1.1"
MAX_SUPPLEMENTARY_REQUESTS_PER_DOCUMENT = 2


class SupplementaryTargetType(str, Enum):
    MISSING_LINE_ITEM = "missing_line_item"
    MISSING_TAX_OR_FEE = "missing_tax_or_fee"
    TOTAL_MISMATCH = "total_mismatch"
    SUBTOTAL_MISMATCH = "subtotal_mismatch"
    QUANTITY_UNIT_PRICE_MISMATCH = "quantity_unit_price_mismatch"
    DATE_AMBIGUITY = "date_ambiguity"
    INVOICE_NUMBER_AMBIGUITY = "invoice_number_ambiguity"
    VENDOR_NAME_AMBIGUITY = "vendor_name_ambiguity"
    PAGE_CONTINUATION = "page_continuation"
    PAID_CROSSED_OUT_ROW_STATUS = "paid_crossed_out_row_status"
    DUPLICATE_ROW_SUSPICION = "duplicate_row_suspicion"


class SupplementaryResolutionKind(str, Enum):
    SCALAR = "scalar"
    LINE_ITEM = "line_item"
    TAX_AMOUNT = "tax_amount"
    FEES_AMOUNT = "fees_amount"
    SHIPPING_AMOUNT = "shipping_amount"
    SUBTOTAL = "subtotal"
    TOTAL_AMOUNT = "total_amount"
    STATUS = "status"
    NONE = "none"


class SupplementaryVisibilityStatus(str, Enum):
    VISIBLE = "visible"
    NOT_VISIBLE = "not_visible"
    AMBIGUOUS = "ambiguous"


class IdentityCandidateType(str, Enum):
    INVOICE_NUMBER = "invoice_number"
    ACCOUNT_NUMBER = "account_number"
    STATEMENT_NUMBER = "statement_number"
    ORDER_NUMBER = "order_number"
    WORK_ORDER = "work_order"
    CUSTOMER_NUMBER = "customer_number"
    UNKNOWN = "unknown"


class SupplementaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SupplementaryTarget(SupplementaryModel):
    target_type: SupplementaryTargetType
    page_number: int | None = None
    field_name: str | None = None
    local_trigger_codes: list[str] = Field(default_factory=list)

    @property
    def target_id(self) -> str:
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:20]


class SupplementaryLineItem(SupplementaryModel):
    source_page: int | None
    section_header: str | None
    row_label: str | None
    location_candidate: str | None
    activity: str | None
    raw_description: str | None
    quantity: str | int | float | Decimal | None
    unit_price: str | int | float | Decimal | None
    amount: str | int | float | Decimal | None
    tax: str | int | float | Decimal | None


class SupplementaryEvidenceReference(SupplementaryModel):
    page_number: int | None
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    crop_id: str | None = None
    crop_role: str | None = None
    plan_id: str | None = None
    packet_sha256: str | None = None
    source_kind: str | None = None
    evidence_kind: str | None = None


class SupplementaryObservedCandidate(SupplementaryModel):
    resolution_kind: SupplementaryResolutionKind
    field_name: str | None
    raw_value: str | None
    line_item: SupplementaryLineItem | None
    evidence_references: list[SupplementaryEvidenceReference] = Field(
        default_factory=list,
    )


class SupplementaryIdentityCandidate(SupplementaryModel):
    raw_candidate: str | None
    adjacent_visible_label: str | None
    candidate_type: IdentityCandidateType
    evidence_reference: SupplementaryEvidenceReference | None
    evidence_references: list[SupplementaryEvidenceReference] = Field(
        default_factory=list,
    )
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    unresolved: bool = False


class SupplementaryFinancialComponents(SupplementaryModel):
    subtotal: str | int | float | Decimal | None = None
    tax: str | int | float | Decimal | None = None
    fees: str | int | float | Decimal | None = None
    credits: str | int | float | Decimal | None = None
    discounts: str | int | float | Decimal | None = None
    previous_balance: str | int | float | Decimal | None = None
    payments: str | int | float | Decimal | None = None
    deposits: str | int | float | Decimal | None = None
    current_charges: str | int | float | Decimal | None = None
    amount_due: str | int | float | Decimal | None = None
    line_item_sum: str | int | float | Decimal | None = None
    total_label: str | None = None
    page_continuation_status: str | None = None
    evidence_references: list[SupplementaryEvidenceReference] = Field(default_factory=list)
    component_evidence_references: Mapping[
        str, list[SupplementaryEvidenceReference]
    ] = Field(default_factory=dict)


class SupplementaryVisibleLabel(SupplementaryModel):
    raw_label: str | None
    visibility_status: SupplementaryVisibilityStatus
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_references: list[SupplementaryEvidenceReference] = Field(
        default_factory=list,
    )


class SupplementaryContradictionObservation(SupplementaryModel):
    raw_candidate: str | None
    observation_kind: str
    visibility_status: SupplementaryVisibilityStatus
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_references: list[SupplementaryEvidenceReference] = Field(
        default_factory=list,
    )


class GeminiSupplementaryObservation(SupplementaryModel):
    target_type: SupplementaryTargetType
    observed_candidate_value: SupplementaryObservedCandidate | None
    raw_visible_text: str | None
    page_number: int | None
    evidence_reference: SupplementaryEvidenceReference | None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    contradiction_flag: bool
    unresolved_flag: bool
    warnings: list[str] = Field(default_factory=list)
    visibility_status: SupplementaryVisibilityStatus = SupplementaryVisibilityStatus.VISIBLE
    observed_candidates: list[SupplementaryIdentityCandidate] = Field(default_factory=list)
    financial_components: SupplementaryFinancialComponents | None = None
    raw_visible_text_evidence_references: list[
        SupplementaryEvidenceReference
    ] = Field(default_factory=list)
    visible_labels: list[SupplementaryVisibleLabel] = Field(default_factory=list)
    contradiction_observations: list[
        SupplementaryContradictionObservation
    ] = Field(default_factory=list)


class SupplementaryFailureStage(str, Enum):
    ENVELOPE = "envelope"
    PAYLOAD_DECODING = "payload_decoding"
    NORMALIZATION = "normalization"
    INTERNAL_CONTRACT = "internal_contract"
    EVIDENCE_REFERENCE = "evidence_reference"
    CROP_REFERENCE = "crop_reference"


class SupplementaryStageStatus(str, Enum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"


class SupplementaryInternalObservationStatus(str, Enum):
    NOT_CONSTRUCTED = "not_constructed"
    CONSTRUCTED = "constructed"
    FAILED = "failed"


class SupplementarySafeDiagnostics(SupplementaryModel):
    """Private-value-free diagnostics safe for experiment telemetry.

    Field values, response bodies, filenames, paths, and visible document text
    are deliberately absent. Unknown field names are represented only by
    hashes so provider-contract defects can be diagnosed without retaining the
    provider payload.
    """

    contract_version: str = "supplementary-safe-diagnostics/1.0"
    stage: SupplementaryFailureStage
    failure_code: str | None = None
    payload_present: bool = False
    payload_byte_length: int | None = Field(default=None, ge=0)
    payload_sha256: str | None = None
    payload_parse_result: str = "not_run"
    decoding_count: int = Field(default=0, ge=0, le=1)
    known_top_level_keys: tuple[str, ...] = ()
    top_level_value_types: Mapping[str, str] = Field(default_factory=dict)
    nested_shape_inventory: Mapping[str, str] = Field(default_factory=dict)
    normalization_actions: tuple[str, ...] = ()
    internal_validation_paths: tuple[str, ...] = ()
    missing_required_fields: tuple[str, ...] = ()
    unexpected_field_name_hashes: tuple[str, ...] = ()
    invalid_enum_categories: tuple[str, ...] = ()
    evidence_reference_validation: str = "not_run"
    crop_reference_validation: str = "not_run"
    transport_validation_status: SupplementaryStageStatus = (
        SupplementaryStageStatus.NOT_RUN
    )
    transport_normalization_status: SupplementaryStageStatus = (
        SupplementaryStageStatus.NOT_RUN
    )
    evidence_validation_status: SupplementaryStageStatus = (
        SupplementaryStageStatus.NOT_RUN
    )
    internal_observation_status: SupplementaryInternalObservationStatus = (
        SupplementaryInternalObservationStatus.NOT_CONSTRUCTED
    )


@dataclass(frozen=True)
class SupplementaryNormalizationResult:
    """Original and normalized objects kept distinct only in process memory."""

    original_payload: Mapping[str, Any] = field(repr=False)
    normalized_payload: Mapping[str, Any] = field(repr=False)
    diagnostics: SupplementarySafeDiagnostics


class SupplementaryVerificationError(ValueError):
    def __init__(
        self,
        failure_code: str,
        *,
        diagnostics: SupplementarySafeDiagnostics | None = None,
    ) -> None:
        super().__init__(failure_code)
        self.failure_code = failure_code
        self.diagnostics = diagnostics


class SupplementaryRequestLimiter:
    """One-pass per-document limit; it has no retry or recursion mechanism."""

    def __init__(self, maximum: int = MAX_SUPPLEMENTARY_REQUESTS_PER_DOCUMENT) -> None:
        self.maximum = max(0, int(maximum))
        self._target_ids: set[str] = set()

    @property
    def request_count(self) -> int:
        return len(self._target_ids)

    def authorize(self, target: SupplementaryTarget) -> None:
        if target.target_id in self._target_ids:
            raise SupplementaryVerificationError("supplementary_target_already_requested")
        if len(self._target_ids) >= self.maximum:
            raise SupplementaryVerificationError("supplementary_request_limit_reached")
        self._target_ids.add(target.target_id)


_FINANCIAL_RESOLUTIONS = {
    SupplementaryResolutionKind.LINE_ITEM,
    SupplementaryResolutionKind.TAX_AMOUNT,
    SupplementaryResolutionKind.FEES_AMOUNT,
    SupplementaryResolutionKind.SHIPPING_AMOUNT,
    SupplementaryResolutionKind.SUBTOTAL,
    SupplementaryResolutionKind.TOTAL_AMOUNT,
    SupplementaryResolutionKind.NONE,
}
_TARGET_RESOLUTIONS: Mapping[SupplementaryTargetType, frozenset[SupplementaryResolutionKind]] = {
    SupplementaryTargetType.MISSING_LINE_ITEM: frozenset({
        SupplementaryResolutionKind.LINE_ITEM, SupplementaryResolutionKind.NONE,
    }),
    SupplementaryTargetType.MISSING_TAX_OR_FEE: frozenset({
        SupplementaryResolutionKind.TAX_AMOUNT,
        SupplementaryResolutionKind.FEES_AMOUNT,
        SupplementaryResolutionKind.SHIPPING_AMOUNT,
        SupplementaryResolutionKind.NONE,
    }),
    SupplementaryTargetType.TOTAL_MISMATCH: frozenset(_FINANCIAL_RESOLUTIONS),
    SupplementaryTargetType.SUBTOTAL_MISMATCH: frozenset(_FINANCIAL_RESOLUTIONS),
    SupplementaryTargetType.QUANTITY_UNIT_PRICE_MISMATCH: frozenset({
        SupplementaryResolutionKind.LINE_ITEM,
        SupplementaryResolutionKind.SCALAR,
        SupplementaryResolutionKind.NONE,
    }),
    SupplementaryTargetType.DATE_AMBIGUITY: frozenset({
        SupplementaryResolutionKind.SCALAR, SupplementaryResolutionKind.NONE,
    }),
    SupplementaryTargetType.INVOICE_NUMBER_AMBIGUITY: frozenset({
        SupplementaryResolutionKind.SCALAR, SupplementaryResolutionKind.NONE,
    }),
    SupplementaryTargetType.VENDOR_NAME_AMBIGUITY: frozenset({
        SupplementaryResolutionKind.SCALAR, SupplementaryResolutionKind.NONE,
    }),
    SupplementaryTargetType.PAGE_CONTINUATION: frozenset({
        SupplementaryResolutionKind.STATUS, SupplementaryResolutionKind.NONE,
    }),
    SupplementaryTargetType.PAID_CROSSED_OUT_ROW_STATUS: frozenset({
        SupplementaryResolutionKind.STATUS, SupplementaryResolutionKind.NONE,
    }),
    SupplementaryTargetType.DUPLICATE_ROW_SUSPICION: frozenset({
        SupplementaryResolutionKind.STATUS, SupplementaryResolutionKind.NONE,
    }),
}


def allowed_supplementary_resolutions(
    target_type: SupplementaryTargetType,
) -> frozenset[SupplementaryResolutionKind]:
    """Single authority for target-to-observable-resolution compatibility."""

    return _TARGET_RESOLUTIONS[target_type]


def select_supplementary_targets(
    initial_facts: Mapping[str, Any], escalation_reasons: list[str],
) -> list[SupplementaryTarget]:
    """Select at most two visual questions solely from local validation codes."""

    if initial_facts.get("supplementary_evidence_revisions"):
        raise SupplementaryVerificationError("recursive_supplementary_verification_forbidden")
    reasons = list(dict.fromkeys(str(item or "") for item in escalation_reasons if item))
    targets: list[SupplementaryTarget] = []

    def add(target: SupplementaryTarget) -> None:
        if target.target_id not in {item.target_id for item in targets}:
            targets.append(target)

    reconciliations = [
        item for item in initial_facts.get("page_reconciliations") or []
        if isinstance(item, Mapping)
    ]
    mismatch_pages = [
        _int_or_none(item.get("page")) for item in reconciliations
        if str(item.get("status") or "").strip().casefold() != "reconciled"
        or abs(_decimal(item.get("difference"))) > Decimal("0.01")
        or abs(
            _decimal(item.get("printed_total")) - _decimal(item.get("component_total"))
        ) > Decimal("0.01")
    ]
    if "payable_rows_missing" in reasons:
        add(SupplementaryTarget(
            target_type=SupplementaryTargetType.MISSING_LINE_ITEM,
            page_number=next((page for page in mismatch_pages if page), 1),
            field_name="line_items",
            local_trigger_codes=["payable_rows_missing"],
        ))
    if any(code in reasons for code in (
        "page_reconciliation_failed", "invoice_reconciliation_failed",
        "financial_content_collapsed", "financial_content_skipped",
    )):
        add(SupplementaryTarget(
            target_type=SupplementaryTargetType.TOTAL_MISMATCH,
            page_number=next((page for page in mismatch_pages if page), 1),
            field_name="reconciliation",
            local_trigger_codes=[code for code in reasons if code in {
                "page_reconciliation_failed", "invoice_reconciliation_failed",
                "financial_content_collapsed", "financial_content_skipped",
            }],
        ))
    if any(code == "required_fact_missing:invoice_number" for code in reasons):
        add(SupplementaryTarget(
            target_type=SupplementaryTargetType.INVOICE_NUMBER_AMBIGUITY,
            page_number=1, field_name="invoice_number",
            local_trigger_codes=["required_fact_missing:invoice_number"],
        ))
    if any(code in reasons for code in ("date_ambiguity", "required_fact_missing:invoice_date")):
        add(SupplementaryTarget(
            target_type=SupplementaryTargetType.DATE_AMBIGUITY,
            page_number=1, field_name="invoice_date",
            local_trigger_codes=[code for code in reasons if "date" in code],
        ))
    if any(code == "required_fact_missing:vendor_name" for code in reasons):
        add(SupplementaryTarget(
            target_type=SupplementaryTargetType.VENDOR_NAME_AMBIGUITY,
            page_number=1, field_name="vendor_name",
            local_trigger_codes=["required_fact_missing:vendor_name"],
        ))
    if any("paid" in code or "crossed_out" in code for code in reasons):
        add(SupplementaryTarget(
            target_type=SupplementaryTargetType.PAID_CROSSED_OUT_ROW_STATUS,
            page_number=1, field_name="paid_status",
            local_trigger_codes=[code for code in reasons if "paid" in code or "crossed" in code],
        ))
    if any("continuation" in code or "carried_forward" in code for code in reasons):
        add(SupplementaryTarget(
            target_type=SupplementaryTargetType.PAGE_CONTINUATION,
            page_number=next((page for page in mismatch_pages if page), 1),
            field_name="page_continuation_status",
            local_trigger_codes=[
                code for code in reasons
                if "continuation" in code or "carried_forward" in code
            ],
        ))
    if any("duplicate" in code for code in reasons):
        add(SupplementaryTarget(
            target_type=SupplementaryTargetType.DUPLICATE_ROW_SUSPICION,
            page_number=1, field_name="duplicate_status",
            local_trigger_codes=[code for code in reasons if "duplicate" in code],
        ))
    return targets[:MAX_SUPPLEMENTARY_REQUESTS_PER_DOCUMENT]


def build_minimized_initial_summary(
    initial_facts: Mapping[str, Any], target: SupplementaryTarget,
) -> dict[str, Any]:
    """Return only the observed facts needed to ask the selected visual question."""

    page = target.page_number
    rows = [
        item for item in initial_facts.get("line_items") or []
        if isinstance(item, Mapping)
        and (page is None or _int_or_none(item.get("source_page")) in {None, page})
    ]
    base = {
        "target_type": target.target_type.value,
        "page_number": page,
        "field_name": target.field_name,
    }
    if target.target_type in {
        SupplementaryTargetType.MISSING_LINE_ITEM,
        SupplementaryTargetType.MISSING_TAX_OR_FEE,
        SupplementaryTargetType.TOTAL_MISMATCH,
        SupplementaryTargetType.SUBTOTAL_MISMATCH,
        SupplementaryTargetType.QUANTITY_UNIT_PRICE_MISMATCH,
    }:
        base.update({
            "observed_rows": [{
                "source_page": _int_or_none(item.get("source_page")),
                "row_label": _text_or_none(item.get("row_label")),
                "activity": _text_or_none(item.get("activity")),
                "raw_description": _text_or_none(
                    item.get("raw_description") or item.get("description")
                ),
                "quantity": _json_number(item.get("quantity")),
                "unit_price": _json_number(item.get("unit_price")),
                "amount": _json_number(item.get("amount")),
            } for item in rows],
            "observed_financial_components": {
                key: _json_number(initial_facts.get(key))
                for key in (
                    "subtotal", "tax_amount", "shipping_amount", "fees_amount", "total_amount"
                )
            },
            "page_reconciliation": [{
                "page": _int_or_none(item.get("page")),
                "component_total": _json_number(item.get("component_total")),
                "printed_total": _json_number(item.get("printed_total")),
                "status": _text_or_none(item.get("status")),
            } for item in initial_facts.get("page_reconciliations") or []
            if isinstance(item, Mapping) and (
                page is None or _int_or_none(item.get("page")) in {None, page}
            )],
        })
    elif target.target_type is SupplementaryTargetType.DATE_AMBIGUITY:
        base["observed_date_candidates"] = [
            {
                "field": _text_or_none(item.get("field")),
                "raw_value": _text_or_none(item.get("raw_value")),
                "provenance": _text_or_none(item.get("provenance")),
            }
            for item in initial_facts.get("observed_date_candidates") or []
            if isinstance(item, Mapping)
        ]
    elif target.target_type is SupplementaryTargetType.INVOICE_NUMBER_AMBIGUITY:
        base["observed_candidate"] = _text_or_none(initial_facts.get("invoice_number"))
    elif target.target_type is SupplementaryTargetType.VENDOR_NAME_AMBIGUITY:
        base["observed_candidate"] = _text_or_none(initial_facts.get("vendor_name"))
    elif target.target_type is SupplementaryTargetType.PAID_CROSSED_OUT_ROW_STATUS:
        base["observed_rows"] = [{
            "source_page": _int_or_none(item.get("source_page")),
            "row_label": _text_or_none(item.get("row_label")),
            "raw_description": _text_or_none(item.get("raw_description") or item.get("description")),
            "amount": _json_number(item.get("amount")),
        } for item in rows]
    return base


def supplementary_response_format(target: SupplementaryTarget) -> dict[str, Any]:
    nullable_string = {"type": ["string", "null"]}
    nullable_number = {"type": ["string", "number", "null"]}
    line_item = {
        "type": ["object", "null"],
        "properties": {
            "source_page": {"type": ["integer", "null"]},
            "section_header": nullable_string,
            "row_label": nullable_string,
            "location_candidate": nullable_string,
            "activity": nullable_string,
            "raw_description": nullable_string,
            "quantity": nullable_number,
            "unit_price": nullable_number,
            "amount": nullable_number,
            "tax": nullable_number,
        },
        "required": [
            "source_page", "section_header", "row_label", "location_candidate",
            "activity", "raw_description", "quantity", "unit_price", "amount", "tax",
        ],
    }
    allowed = sorted(
        item.value for item in allowed_supplementary_resolutions(target.target_type)
    )
    candidate = {
        "type": ["object", "null"],
        "properties": {
            "resolution_kind": {"type": "string", "enum": allowed},
            "field_name": nullable_string,
            "raw_value": nullable_string,
            "line_item": line_item,
        },
        "required": [
            "resolution_kind", "field_name", "raw_value", "line_item",
        ],
    }
    evidence = {
        "type": ["object", "null"],
        "properties": {
            "page_number": {"type": ["integer", "null"]},
            "bbox": {"type": ["array", "null"], "items": {"type": "number"}},
            "crop_id": nullable_string,
            "crop_role": nullable_string,
        },
        "required": ["page_number", "bbox", "crop_id", "crop_role"],
    }
    identity_candidate = {
        "type": "object",
        "properties": {
            "raw_candidate": nullable_string,
            "adjacent_visible_label": nullable_string,
            "candidate_type": {"type": "string", "enum": [item.value for item in IdentityCandidateType]},
            "evidence_reference": evidence,
            "confidence": {"type": ["number", "null"]},
            "unresolved": {"type": "boolean"},
        },
        "required": [
            "raw_candidate", "adjacent_visible_label", "candidate_type",
            "evidence_reference", "confidence", "unresolved",
        ],
    }
    financial_components = {
        "type": ["object", "null"],
        "properties": {
            **{key: nullable_number for key in (
                "subtotal", "tax", "fees", "credits", "discounts",
                "previous_balance", "payments", "deposits", "current_charges",
                "amount_due", "line_item_sum",
            )},
            "total_label": nullable_string,
            "page_continuation_status": nullable_string,
            "evidence_references": {"type": "array", "items": evidence},
        },
        "required": [
            "subtotal", "tax", "fees", "credits", "discounts", "previous_balance",
            "payments", "deposits", "current_charges", "amount_due", "line_item_sum",
            "total_label", "page_continuation_status", "evidence_references",
        ],
    }
    schema = {
        "type": "object",
        "properties": {
            "target_type": {"type": "string", "enum": [target.target_type.value]},
            "observed_candidate_value": candidate,
            "raw_visible_text": nullable_string,
            "page_number": {"type": ["integer", "null"]},
            "evidence_reference": evidence,
            "confidence": {"type": ["number", "null"]},
            "contradiction_flag": {"type": "boolean"},
            "unresolved_flag": {"type": "boolean"},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "visibility_status": {
                "type": "string", "enum": [item.value for item in SupplementaryVisibilityStatus],
            },
            "observed_candidates": {"type": "array", "items": identity_candidate},
            "financial_components": financial_components,
        },
        "required": [
            "target_type", "observed_candidate_value", "raw_visible_text", "page_number",
            "evidence_reference", "confidence", "contradiction_flag", "unresolved_flag",
            "warnings",
            "visibility_status", "observed_candidates", "financial_components",
        ],
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": f"innerview_supplementary_{target.target_type.value}",
            "strict": True,
            "schema": schema,
        },
    }


def build_supplementary_prompt(
    *, opaque_document_id: str, target: SupplementaryTarget,
    minimized_summary: Mapping[str, Any], evidence_plan_summary: Mapping[str, Any] | None = None,
) -> str:
    return "\n".join((
        "Verify exactly one unresolved visual fact in the supplied source page.",
        "Return JSON only using the response schema. Do not inspect or repair the whole invoice.",
        "Report only what is visibly supported. Use unresolved_flag=true and null when evidence is insufficient.",
        "Each supplied image has an explicit crop_id and role. Link observations to those crop IDs.",
        "Preserve multiple visible identity candidates and their adjacent labels; never force a single identity.",
        "For total composition, observe subtotal, tax, fees, credits, discounts, previous balance, payments, deposits, current charges, amount due, line-item sum, total label, and continuation independently.",
        "Never infer or invent a component merely because it would make arithmetic reconcile.",
        "Do not return GL accounts, accounting policy, readiness, export, labels, corrections, learning, rules, or rationale.",
        f"Opaque experiment document ID: {opaque_document_id}",
        f"Exact target: {target.target_type.value}",
        f"Target page: {target.page_number if target.page_number is not None else 'unknown'}",
        "Validated evidence plan:",
        json.dumps(dict(evidence_plan_summary or {}), sort_keys=True, separators=(",", ":"), default=str),
        "Minimized initial observation:",
        json.dumps(dict(minimized_summary), sort_keys=True, separators=(",", ":"), default=str),
    ))


_KNOWN_FIELD_ALIASES: Mapping[str, str] = {
    # Native structured-output implementations occasionally return the exact
    # schema names in camelCase. These aliases are one-to-one spellings of the
    # same field; no accounting or semantic interpretation is performed.
    "targetType": "target_type",
    "observedCandidateValue": "observed_candidate_value",
    "rawVisibleText": "raw_visible_text",
    "pageNumber": "page_number",
    "evidenceReference": "evidence_reference",
    "contradictionFlag": "contradiction_flag",
    "unresolvedFlag": "unresolved_flag",
    "visibilityStatus": "visibility_status",
    "observedCandidates": "observed_candidates",
    "financialComponents": "financial_components",
    "resolutionKind": "resolution_kind",
    "fieldName": "field_name",
    "rawValue": "raw_value",
    "lineItem": "line_item",
    "sourcePage": "source_page",
    "sectionHeader": "section_header",
    "rowLabel": "row_label",
    "locationCandidate": "location_candidate",
    "rawDescription": "raw_description",
    "unitPrice": "unit_price",
    "cropId": "crop_id",
    "cropRole": "crop_role",
    "rawCandidate": "raw_candidate",
    "adjacentVisibleLabel": "adjacent_visible_label",
    "candidateType": "candidate_type",
    "totalLabel": "total_label",
    "pageContinuationStatus": "page_continuation_status",
    "evidenceReferences": "evidence_references",
    "componentEvidenceReferences": "component_evidence_references",
    "rawVisibleTextEvidenceReferences": "raw_visible_text_evidence_references",
    "visibleLabels": "visible_labels",
    "contradictionObservations": "contradiction_observations",
    "rawLabel": "raw_label",
    "observationKind": "observation_kind",
    "planId": "plan_id",
    "packetSha256": "packet_sha256",
    "sourceKind": "source_kind",
    "evidenceKind": "evidence_kind",
    "previousBalance": "previous_balance",
    "currentCharges": "current_charges",
    "amountDue": "amount_due",
    "lineItemSum": "line_item_sum",
}
_KNOWN_FIELDS = frozenset({
    *GeminiSupplementaryObservation.model_fields,
    *SupplementaryObservedCandidate.model_fields,
    *SupplementaryLineItem.model_fields,
    *SupplementaryEvidenceReference.model_fields,
    *SupplementaryIdentityCandidate.model_fields,
    *SupplementaryFinancialComponents.model_fields,
    *SupplementaryVisibleLabel.model_fields,
    *SupplementaryContradictionObservation.model_fields,
})
_REQUIRED_TOP_LEVEL_FIELDS = frozenset({
    "target_type", "observed_candidate_value", "raw_visible_text", "page_number",
    "evidence_reference", "confidence", "contradiction_flag", "unresolved_flag",
    "warnings", "visibility_status", "observed_candidates", "financial_components",
})
_NULLABLE_TEXT_FIELDS = frozenset({
    "raw_visible_text", "field_name", "raw_value", "section_header", "row_label",
    "location_candidate", "activity", "raw_description", "crop_id", "crop_role",
    "raw_candidate", "adjacent_visible_label", "total_label",
    "page_continuation_status",
    "plan_id", "packet_sha256", "source_kind", "evidence_kind", "raw_label",
})
_DECIMAL_FIELDS = frozenset({
    "quantity", "unit_price", "amount", "tax", "subtotal", "fees", "credits",
    "discounts", "previous_balance", "payments", "deposits", "current_charges",
    "amount_due", "line_item_sum",
})
_INTEGER_FIELDS = frozenset({"page_number", "source_page"})
_ENUM_VALUES: Mapping[str, tuple[str, ...]] = {
    "target_type": tuple(item.value for item in SupplementaryTargetType),
    "resolution_kind": tuple(item.value for item in SupplementaryResolutionKind),
    "visibility_status": tuple(item.value for item in SupplementaryVisibilityStatus),
    "candidate_type": tuple(item.value for item in IdentityCandidateType),
}


def _safe_value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "array"
    if isinstance(value, (int, float, Decimal)):
        return "number"
    return "other"


def _field_name_hash(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()


def _safe_shape_inventory(value: Any, path: tuple[str, ...] = ()) -> dict[str, str]:
    inventory: dict[str, str] = {}
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = _KNOWN_FIELD_ALIASES.get(str(raw_key), str(raw_key))
            if key not in _KNOWN_FIELDS:
                continue
            child_path = (*path, key)
            rendered = ".".join(child_path)
            inventory[rendered] = _safe_value_type(child)
            inventory.update(_safe_shape_inventory(child, child_path))
    elif isinstance(value, list):
        for child in value:
            inventory.update(_safe_shape_inventory(child, (*path, "[]")))
    return dict(sorted(inventory.items()))


def _diagnostics_for_payload(payload: Mapping[str, Any]) -> SupplementarySafeDiagnostics:
    known = tuple(sorted(
        _KNOWN_FIELD_ALIASES.get(str(key), str(key))
        for key in payload
        if _KNOWN_FIELD_ALIASES.get(str(key), str(key)) in _KNOWN_FIELDS
    ))
    unexpected = tuple(sorted(
        _field_name_hash(key)
        for key in payload
        if _KNOWN_FIELD_ALIASES.get(str(key), str(key)) not in _KNOWN_FIELDS
    ))
    return SupplementarySafeDiagnostics(
        stage=SupplementaryFailureStage.NORMALIZATION,
        payload_present=True,
        payload_parse_result="object_decoded",
        decoding_count=1,
        known_top_level_keys=known,
        top_level_value_types={
            _KNOWN_FIELD_ALIASES.get(str(key), str(key)): _safe_value_type(value)
            for key, value in payload.items()
            if _KNOWN_FIELD_ALIASES.get(str(key), str(key)) in _KNOWN_FIELDS
        },
        nested_shape_inventory=_safe_shape_inventory(payload),
        unexpected_field_name_hashes=unexpected,
    )


def _canonical_enum(field_name: str, value: Any) -> tuple[Any, bool]:
    if not isinstance(value, str) or field_name not in _ENUM_VALUES:
        return value, False
    comparable = value.strip().casefold().replace("-", "_").replace(" ", "_")
    matches = [item for item in _ENUM_VALUES[field_name] if item.casefold() == comparable]
    if len(matches) == 1:
        return matches[0], matches[0] != value
    return value, False


def _decimal_value(value: Any) -> tuple[Any, bool]:
    if not isinstance(value, str):
        return value, False
    stripped = value.strip()
    if not stripped:
        return None, True
    try:
        decimal_value = Decimal(stripped.replace(",", ""))
    except InvalidOperation:
        return value, False
    if not decimal_value.is_finite():
        return value, False
    return decimal_value, True


def _integer_value(value: Any) -> tuple[Any, bool]:
    if not isinstance(value, str):
        return value, False
    stripped = value.strip()
    if not stripped:
        return None, True
    try:
        parsed = int(stripped)
    except ValueError:
        return value, False
    return parsed, True


def _normalize_provider_node(
    value: Any, *, path: tuple[str, ...], actions: list[str],
) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, child in value.items():
            raw_name = str(raw_key)
            key = _KNOWN_FIELD_ALIASES.get(raw_name, raw_name)
            if key in normalized and raw_name != key:
                diagnostics = _diagnostics_for_payload(value if not path else {})
                diagnostics = diagnostics.model_copy(update={
                    "stage": SupplementaryFailureStage.NORMALIZATION,
                    "failure_code": "supplementary_internal_contract_invalid",
                    "internal_validation_paths": (".".join((*path, key)),),
                })
                raise SupplementaryVerificationError(
                    "supplementary_internal_contract_invalid", diagnostics=diagnostics,
                )
            if key != raw_name:
                actions.append(f"alias:{key}")
            normalized[key] = _normalize_provider_node(
                child, path=(*path, key), actions=actions,
            )
        return normalized
    if isinstance(value, list):
        return [
            _normalize_provider_node(item, path=(*path, "[]"), actions=actions)
            for item in value
        ]
    field_name = next((item for item in reversed(path) if item != "[]"), "")
    if isinstance(value, str) and not value.strip() and field_name in _NULLABLE_TEXT_FIELDS:
        actions.append(f"blank_to_null:{field_name}")
        return None
    if field_name in _DECIMAL_FIELDS:
        normalized, changed = _decimal_value(value)
        if changed:
            actions.append(f"numeric_string:{field_name}")
        return normalized
    if field_name in _INTEGER_FIELDS:
        normalized, changed = _integer_value(value)
        if changed:
            actions.append(f"integer_string:{field_name}")
        return normalized
    if field_name in _ENUM_VALUES:
        normalized, changed = _canonical_enum(field_name, value)
        if changed:
            actions.append(f"enum:{field_name}")
        return normalized
    return value


def normalize_supplementary_provider_payload(
    payload: Mapping[str, Any],
) -> SupplementaryNormalizationResult:
    """Normalize only lossless, one-to-one provider representation variants."""

    diagnostics = _diagnostics_for_payload(payload)
    actions: list[str] = []
    normalized = _normalize_provider_node(payload, path=(), actions=actions)
    missing = tuple(sorted(_REQUIRED_TOP_LEVEL_FIELDS - set(normalized)))
    diagnostics = diagnostics.model_copy(update={
        "normalization_actions": tuple(sorted(set(actions))),
        "missing_required_fields": missing,
    })
    return SupplementaryNormalizationResult(
        original_payload=copy.deepcopy(dict(payload)),
        normalized_payload=normalized,
        diagnostics=diagnostics,
    )


def _safe_validation_diagnostics(
    result: SupplementaryNormalizationResult, exc: ValidationError,
) -> SupplementarySafeDiagnostics:
    paths: set[str] = set()
    missing: set[str] = set(result.diagnostics.missing_required_fields)
    unexpected_hashes: set[str] = set(result.diagnostics.unexpected_field_name_hashes)
    enum_categories: set[str] = set()
    has_type_error = False
    has_evidence_error = False
    has_extra_error = False
    for error in exc.errors(include_input=False, include_url=False):
        location = error.get("loc") or ()
        safe_parts: list[str] = []
        for part in location:
            if isinstance(part, int):
                safe_parts.append("[]")
            elif str(part) in _KNOWN_FIELDS:
                safe_parts.append(str(part))
            else:
                unexpected_hashes.add(_field_name_hash(part))
                safe_parts.append("<unexpected_field_hash>")
        path = ".".join(safe_parts)
        if path:
            paths.add(path)
        error_type = str(error.get("type") or "")
        leaf = next((item for item in reversed(safe_parts) if item not in {"[]", "<unexpected_field_hash>"}), "")
        if error_type == "missing" and leaf:
            missing.add(leaf)
        if error_type == "enum":
            enum_categories.add({
                "target_type": "SupplementaryTargetType",
                "resolution_kind": "SupplementaryResolutionKind",
                "visibility_status": "SupplementaryVisibilityStatus",
                "candidate_type": "IdentityCandidateType",
            }.get(leaf, "supplementary_enum"))
        if "type" in error_type or error_type.endswith("_parsing"):
            has_type_error = True
        if "evidence_reference" in safe_parts or "bbox" in safe_parts:
            has_evidence_error = True
        if error_type == "extra_forbidden":
            has_extra_error = True
    if missing:
        code = "supplementary_required_field_missing"
    elif enum_categories:
        code = "supplementary_enum_invalid"
    elif has_evidence_error:
        code = "supplementary_evidence_reference_invalid"
    elif has_type_error:
        code = "supplementary_field_type_invalid"
    elif has_extra_error:
        code = "supplementary_internal_contract_invalid"
    else:
        code = "supplementary_internal_contract_invalid"
    return result.diagnostics.model_copy(update={
        "stage": (
            SupplementaryFailureStage.EVIDENCE_REFERENCE
            if has_evidence_error else SupplementaryFailureStage.INTERNAL_CONTRACT
        ),
        "failure_code": code,
        "internal_validation_paths": tuple(sorted(paths)),
        "missing_required_fields": tuple(sorted(missing)),
        "unexpected_field_name_hashes": tuple(sorted(unexpected_hashes)),
        "invalid_enum_categories": tuple(sorted(enum_categories)),
        "evidence_reference_validation": "invalid" if has_evidence_error else "not_run",
    })


def parse_decoded_supplementary_payload(
    payload: Mapping[str, Any], *, target: SupplementaryTarget,
) -> tuple[GeminiSupplementaryObservation, SupplementarySafeDiagnostics]:
    result = normalize_supplementary_provider_payload(payload)
    if result.diagnostics.missing_required_fields:
        diagnostics = result.diagnostics.model_copy(update={
            "stage": SupplementaryFailureStage.INTERNAL_CONTRACT,
            "failure_code": "supplementary_required_field_missing",
        })
        raise SupplementaryVerificationError(
            "supplementary_required_field_missing", diagnostics=diagnostics,
        )
    try:
        observation = GeminiSupplementaryObservation.model_validate(
            result.normalized_payload,
        )
    except ValidationError as exc:
        diagnostics = _safe_validation_diagnostics(result, exc)
        raise SupplementaryVerificationError(
            diagnostics.failure_code or "supplementary_internal_contract_invalid",
            diagnostics=diagnostics,
        ) from exc
    diagnostics = result.diagnostics.model_copy(update={
        "stage": SupplementaryFailureStage.INTERNAL_CONTRACT,
        "failure_code": None,
        "evidence_reference_validation": "shape_valid",
    })
    if observation.target_type is not target.target_type:
        diagnostics = diagnostics.model_copy(update={
            "failure_code": "supplementary_target_mismatch",
            "invalid_enum_categories": ("SupplementaryTargetType",),
        })
        raise SupplementaryVerificationError(
            "supplementary_target_mismatch", diagnostics=diagnostics,
        )
    return observation, diagnostics


def validate_supplementary_observation(
    observation: GeminiSupplementaryObservation,
    *,
    target: SupplementaryTarget,
    diagnostics: SupplementarySafeDiagnostics | None = None,
) -> GeminiSupplementaryObservation:
    """Enforce target-specific semantics after strict structural validation."""

    candidate = observation.observed_candidate_value
    if observation.unresolved_flag:
        return observation
    if candidate is None or candidate.resolution_kind is SupplementaryResolutionKind.NONE:
        raise SupplementaryVerificationError(
            "supplementary_resolved_without_candidate", diagnostics=diagnostics,
        )
    if candidate.resolution_kind not in allowed_supplementary_resolutions(
        target.target_type,
    ):
        raise SupplementaryVerificationError(
            "supplementary_resolution_not_allowed", diagnostics=diagnostics,
        )
    if candidate.resolution_kind is SupplementaryResolutionKind.LINE_ITEM:
        if candidate.line_item is None:
            raise SupplementaryVerificationError(
                "supplementary_line_item_missing", diagnostics=diagnostics,
            )
    elif candidate.line_item is not None and _line_item_has_observed_value(candidate.line_item):
        # Some structured-output implementations materialize every branch of a
        # nullable object.  A non-empty incompatible branch is not discarded or
        # treated as a transport failure: preserve it as an explicit visual
        # contradiction so the document remains review-required and blocked.
        observation = observation.model_copy(update={
            "contradiction_flag": True,
            "warnings": list(dict.fromkeys([
                *observation.warnings,
                "supplementary_incompatible_candidate_branches",
            ])),
        })
    if observation.evidence_reference is None or (
        observation.evidence_reference.bbox is None and not observation.raw_visible_text
    ):
        evidence_diagnostics = diagnostics.model_copy(update={
            "stage": SupplementaryFailureStage.EVIDENCE_REFERENCE,
            "failure_code": "supplementary_evidence_required",
            "evidence_reference_validation": "missing",
        }) if diagnostics is not None else None
        raise SupplementaryVerificationError(
            "supplementary_evidence_required",
            diagnostics=evidence_diagnostics,
        )
    return observation


def parse_supplementary_response(
    raw_response: str, *, target: SupplementaryTarget,
) -> GeminiSupplementaryObservation:
    try:
        parsed = extract_single_json_object(raw_response)
    except Exception as exc:
        raise SupplementaryVerificationError("supplementary_invalid_json") from exc
    observation, diagnostics = parse_decoded_supplementary_payload(parsed, target=target)
    return validate_supplementary_observation(
        observation, target=target, diagnostics=diagnostics,
    )


def validate_observation_crop_references(
    observation: GeminiSupplementaryObservation,
    *,
    allowed_crop_ids: set[str],
    planned_crops: Mapping[str, Mapping[str, Any]] | None = None,
    expected_packet_sha256: str | None = None,
    actual_packet_sha256: str | None = None,
) -> None:
    """Ensure provider evidence refers only to the locally approved packet."""

    if (
        expected_packet_sha256 is not None
        and actual_packet_sha256 is not None
        and expected_packet_sha256 != actual_packet_sha256
    ):
        raise SupplementaryVerificationError("supplementary_evidence_reference_invalid")
    if planned_crops is not None:
        if set(planned_crops) != set(allowed_crop_ids):
            raise SupplementaryVerificationError("supplementary_evidence_reference_invalid")
        ordinals = [
            int(value.get("ordinal"))
            for value in planned_crops.values()
            if value.get("ordinal") is not None
        ]
        if sorted(ordinals) != list(range(len(planned_crops))):
            raise SupplementaryVerificationError("supplementary_evidence_reference_invalid")

    references: list[tuple[SupplementaryEvidenceReference, str]] = []
    if observation.evidence_reference is not None:
        references.append((observation.evidence_reference, "observation"))
    references.extend(
        (candidate.evidence_reference, "identity_candidate")
        for candidate in observation.observed_candidates
        if candidate.evidence_reference is not None
    )
    if observation.observed_candidate_value is not None:
        references.extend(
            (reference, "primary_observation")
            for reference in observation.observed_candidate_value.evidence_references
        )
    references.extend(
        (reference, "raw_visible_text")
        for reference in observation.raw_visible_text_evidence_references
    )
    for label in observation.visible_labels:
        references.extend(
            (reference, "visible_label")
            for reference in label.evidence_references
        )
    for contradiction in observation.contradiction_observations:
        references.extend(
            (reference, "contradiction")
            for reference in contradiction.evidence_references
        )
    if observation.financial_components is not None:
        references.extend(
            (reference, "financial_component")
            for reference in observation.financial_components.evidence_references
        )
        for component_references in (
            observation.financial_components.component_evidence_references.values()
        ):
            references.extend(
                (reference, "financial_component")
                for reference in component_references
            )
    for candidate in observation.observed_candidates:
        if (
            candidate.raw_candidate
            and not candidate.unresolved
            and candidate.evidence_reference is None
        ):
            raise SupplementaryVerificationError(
                "supplementary_evidence_reference_invalid",
            )
    if observation.financial_components is not None:
        financial_values = observation.financial_components.model_dump(
            exclude={"evidence_references", "total_label", "page_continuation_status"},
        )
        if (
            any(value not in (None, "") for value in financial_values.values())
            and not observation.financial_components.evidence_references
        ):
            raise SupplementaryVerificationError(
                "supplementary_evidence_reference_invalid",
            )
    for reference, _reference_kind in references:
        if not reference.crop_id:
            if not observation.unresolved_flag:
                raise SupplementaryVerificationError("supplementary_crop_reference_required")
            continue
        if reference.crop_id not in allowed_crop_ids:
            raise SupplementaryVerificationError("supplementary_unplanned_crop_reference")
        if planned_crops is not None:
            planned = planned_crops[reference.crop_id]
            expected_role = str(planned.get("role") or "").strip()
            observed_role = str(reference.crop_role or "").strip()
            if expected_role and observed_role != expected_role:
                raise SupplementaryVerificationError(
                    "supplementary_evidence_reference_invalid",
                )
            expected_page = planned.get("page_number")
            if expected_page is not None and reference.page_number != expected_page:
                raise SupplementaryVerificationError(
                    "supplementary_evidence_reference_invalid",
                )
            expected_packet = planned.get("packet_sha256") or expected_packet_sha256
            if (
                expected_packet
                and reference.packet_sha256 is not None
                and reference.packet_sha256 != expected_packet
            ):
                raise SupplementaryVerificationError(
                    "supplementary_evidence_reference_invalid",
                )


def merge_supplementary_observations(
    initial_facts: Mapping[str, Any],
    targeted_observations: list[tuple[SupplementaryTarget, GeminiSupplementaryObservation]],
) -> dict[str, Any]:
    """Create an effective copy while retaining every initial/supplementary distinction."""

    result = copy.deepcopy(dict(initial_facts))
    before = reconciliation_snapshot(initial_facts)
    revisions: list[dict[str, Any]] = []
    warnings = list(result.get("warnings") or [])
    unresolved = list(result.get("unresolved_visual_regions") or [])
    local_conflict = False

    for revision_index, (target, observation) in enumerate(targeted_observations, start=1):
        revision = {
            "schema_version": SUPPLEMENTARY_SCHEMA_VERSION,
            "prompt_version": SUPPLEMENTARY_PROMPT_VERSION,
            "revision_number": revision_index,
            "target_id": target.target_id,
            "target": target.model_dump(mode="json"),
            "observation": observation.model_dump(mode="json"),
            "source_role": "supplementary_visual_observation",
        }
        revisions.append(revision)
        evidence_row = _observation_evidence(target, observation)
        if evidence_row not in result.setdefault("evidence", []):
            result["evidence"].append(evidence_row)
        if observation.unresolved_flag:
            warnings.append(f"supplementary_unresolved:{target.target_type.value}")
            unresolved.append({
                "page": observation.page_number or target.page_number,
                "field": target.field_name or target.target_type.value,
                "bbox": (
                    list(observation.evidence_reference.bbox)
                    if observation.evidence_reference and observation.evidence_reference.bbox
                    else None
                ),
                "reason": "supplementary_visual_evidence_unresolved",
                "confidence": observation.confidence,
            })
            continue
        if observation.contradiction_flag:
            local_conflict = True
            warnings.append(f"supplementary_contradiction:{target.target_type.value}")
            unresolved.append({
                "page": observation.page_number or target.page_number,
                "field": target.field_name or target.target_type.value,
                "bbox": (
                    list(observation.evidence_reference.bbox)
                    if observation.evidence_reference and observation.evidence_reference.bbox
                    else None
                ),
                "reason": "supplementary_visual_evidence_contradiction",
                "confidence": observation.confidence,
            })
            continue
        candidate = observation.observed_candidate_value
        if candidate is None:
            local_conflict = True
            warnings.append(f"supplementary_missing_candidate:{target.target_type.value}")
            continue
        merged = _merge_candidate(result, target, observation, candidate)
        if merged == "contradiction":
            local_conflict = True
            warnings.append(f"supplementary_local_contradiction:{target.target_type.value}")
        elif merged == "applied":
            warnings.append(f"supplementary_verification_applied:{target.target_type.value}")
        else:
            warnings.append(f"supplementary_verification_confirmed:{target.target_type.value}")

    result["supplementary_evidence_revisions"] = revisions
    result["supplementary_schema_version"] = SUPPLEMENTARY_SCHEMA_VERSION
    result["supplementary_prompt_version"] = SUPPLEMENTARY_PROMPT_VERSION
    result["initial_observed_facts_sha256"] = _stable_hash(initial_facts)
    result["warnings"] = list(dict.fromkeys(warnings))
    result["unresolved_visual_regions"] = unresolved
    after = _rerun_reconciliation(result)
    result["supplementary_reconciliation"] = {
        "before": before,
        "after": after,
        "resolved": bool(after["reconciled"] and not local_conflict),
    }
    if after["reconciled"] and not local_conflict and not unresolved:
        result["visual_extraction_status"] = "complete"
    else:
        result["visual_extraction_status"] = "partial"
    result["needs_manual_review"] = bool(
        local_conflict or unresolved or not after["reconciled"]
    )
    return result


def reconciliation_snapshot(facts: Mapping[str, Any]) -> dict[str, Any]:
    rows = [item for item in facts.get("line_items") or [] if isinstance(item, Mapping)]
    line_total = sum((_decimal(item.get("amount")) for item in rows), Decimal("0"))
    adders = sum((
        _decimal(facts.get(key))
        for key in ("tax_amount", "shipping_amount", "fees_amount")
    ), Decimal("0"))
    total = _decimal_or_none(facts.get("total_amount"))
    difference = None if total is None else total - line_total - adders
    return {
        "reconciled": bool(total is not None and abs(difference or Decimal("0")) <= Decimal("0.01")),
        "difference": str(difference) if difference is not None else None,
        "line_count": len(rows),
    }


def _merge_candidate(
    result: dict[str, Any], target: SupplementaryTarget,
    observation: GeminiSupplementaryObservation,
    candidate: SupplementaryObservedCandidate,
) -> str:
    kind = candidate.resolution_kind
    if kind is SupplementaryResolutionKind.LINE_ITEM:
        assert candidate.line_item is not None
        item = candidate.line_item
        evidence = [{
            "page": observation.page_number or item.source_page or target.page_number,
            "text": observation.raw_visible_text,
            "normalized_text": None,
            "bbox": (
                list(observation.evidence_reference.bbox)
                if observation.evidence_reference and observation.evidence_reference.bbox
                else None
            ),
            "source_type": "supplementary_visual_observation",
            "extraction_method": "gemini_supplementary_verification",
            "confidence": observation.confidence,
        }]
        row = {
            "source_page": item.source_page or observation.page_number or target.page_number,
            "section_header": item.section_header,
            "row_label": item.row_label,
            "location_candidate": item.location_candidate,
            "activity": item.activity,
            "description": item.raw_description,
            "raw_description": item.raw_description,
            "normalized_description": None,
            "generated_description": None,
            "quantity": _decimal_or_none(item.quantity),
            "unit_price": _decimal_or_none(item.unit_price),
            "amount": _decimal_or_none(item.amount),
            "tax": _decimal_or_none(item.tax),
            "confidence": observation.confidence,
            "evidence": evidence,
            "supplementary_target_id": target.target_id,
        }
        fingerprint = _row_fingerprint(row)
        existing = {
            _row_fingerprint(existing_row)
            for existing_row in result.get("line_items") or []
            if isinstance(existing_row, Mapping)
        }
        if fingerprint in existing:
            return "confirmed"
        result.setdefault("line_items", []).append(row)
        return "applied"

    field_name = _candidate_field(target, candidate)
    if not field_name:
        # Status-only observations remain a separately-provenanced revision and
        # intentionally do not mutate financial rows or exclusions.
        return "confirmed"
    value = candidate.raw_value
    if field_name in {"subtotal", "tax_amount", "shipping_amount", "fees_amount", "total_amount"}:
        value = _decimal_or_none(value)
    current = result.get(field_name)
    if current in (None, ""):
        result[field_name] = value
        return "applied"
    return "confirmed" if _values_equivalent(current, value) else "contradiction"


def _candidate_field(
    target: SupplementaryTarget, candidate: SupplementaryObservedCandidate,
) -> str | None:
    if candidate.resolution_kind in {
        SupplementaryResolutionKind.TAX_AMOUNT,
        SupplementaryResolutionKind.FEES_AMOUNT,
        SupplementaryResolutionKind.SHIPPING_AMOUNT,
        SupplementaryResolutionKind.SUBTOTAL,
        SupplementaryResolutionKind.TOTAL_AMOUNT,
    }:
        return candidate.resolution_kind.value
    if candidate.resolution_kind is SupplementaryResolutionKind.SCALAR:
        allowed = {
            SupplementaryTargetType.DATE_AMBIGUITY: {"invoice_date", "service_date", "due_date"},
            SupplementaryTargetType.INVOICE_NUMBER_AMBIGUITY: {"invoice_number"},
            SupplementaryTargetType.VENDOR_NAME_AMBIGUITY: {"vendor_name"},
            SupplementaryTargetType.QUANTITY_UNIT_PRICE_MISMATCH: set(),
        }.get(target.target_type, set())
        field = candidate.field_name or target.field_name
        if field not in allowed:
            raise SupplementaryVerificationError("supplementary_scalar_field_not_allowed")
        return field
    return None


def _observation_evidence(
    target: SupplementaryTarget, observation: GeminiSupplementaryObservation,
) -> dict[str, Any]:
    return {
        "page": observation.page_number or target.page_number,
        "text": observation.raw_visible_text,
        "normalized_text": None,
        "bbox": (
            list(observation.evidence_reference.bbox)
            if observation.evidence_reference and observation.evidence_reference.bbox
            else None
        ),
        "source_type": "supplementary_visual_observation",
        "extraction_method": "gemini_supplementary_verification",
        "confidence": observation.confidence,
        "supplementary_target_id": target.target_id,
    }


def _rerun_reconciliation(result: dict[str, Any]) -> dict[str, Any]:
    snapshot = reconciliation_snapshot(result)
    rows = [item for item in result.get("line_items") or [] if isinstance(item, Mapping)]
    by_page: dict[int, Decimal] = {}
    for row in rows:
        page = _int_or_none(row.get("source_page")) or 1
        by_page[page] = by_page.get(page, Decimal("0")) + _decimal(row.get("amount"))
    reconciliations = []
    for original in result.get("page_reconciliations") or []:
        if not isinstance(original, Mapping):
            continue
        row = dict(original)
        page = _int_or_none(row.get("page")) or 1
        component_total = by_page.get(page, Decimal("0"))
        if len(by_page) <= 1:
            component_total += sum((
                _decimal(result.get(key))
                for key in ("tax_amount", "shipping_amount", "fees_amount")
            ), Decimal("0"))
        printed_total = _decimal_or_none(row.get("printed_total"))
        difference = None if printed_total is None else printed_total - component_total
        row["component_total"] = component_total
        row["difference"] = difference
        row["status"] = (
            "reconciled"
            if difference is not None and abs(difference) <= Decimal("0.01")
            else "mismatch"
        )
        reconciliations.append(row)
    if reconciliations:
        result["page_reconciliations"] = reconciliations
    if snapshot["difference"] is not None and not snapshot["reconciled"]:
        result["unexplained_invoice_difference"] = Decimal(snapshot["difference"])
    else:
        result.pop("unexplained_invoice_difference", None)
    return snapshot


def _row_fingerprint(row: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(_int_or_none(row.get("source_page")) or ""),
        str(row.get("row_label") or "").strip().casefold(),
        str(row.get("activity") or "").strip().casefold(),
        str(row.get("raw_description") or row.get("description") or "").strip().casefold(),
        str(_decimal_or_none(row.get("amount")) or ""),
    )


def _line_item_has_observed_value(line: SupplementaryLineItem) -> bool:
    return any(
        value not in (None, "")
        for value in line.model_dump(mode="python").values()
    )


def _stable_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _values_equivalent(left: Any, right: Any) -> bool:
    left_number = _decimal_or_none(left)
    right_number = _decimal_or_none(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def _decimal(value: Any) -> Decimal:
    return _decimal_or_none(value) or Decimal("0")


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value).replace("$", "").replace(",", "").strip())
        return parsed if parsed.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _json_number(value: Any) -> str | None:
    parsed = _decimal_or_none(value)
    return str(parsed) if parsed is not None else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _text_or_none(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


__all__ = [
    "GeminiSupplementaryObservation", "MAX_SUPPLEMENTARY_REQUESTS_PER_DOCUMENT",
    "SUPPLEMENTARY_PROMPT_VERSION", "SUPPLEMENTARY_SCHEMA_VERSION",
    "SupplementaryFailureStage", "SupplementaryNormalizationResult",
    "SupplementaryObservedCandidate", "SupplementaryRequestLimiter",
    "SupplementaryResolutionKind", "SupplementaryTarget", "SupplementaryTargetType",
    "SupplementarySafeDiagnostics", "SupplementaryVerificationError",
    "allowed_supplementary_resolutions",
    "build_minimized_initial_summary",
    "build_supplementary_prompt", "merge_supplementary_observations",
    "normalize_supplementary_provider_payload",
    "parse_decoded_supplementary_payload", "parse_supplementary_response",
    "reconciliation_snapshot",
    "select_supplementary_targets", "supplementary_response_format",
    "validate_supplementary_observation",
]
