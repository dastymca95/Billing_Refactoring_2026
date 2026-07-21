"""Versioned Gemini supplementary response transport contracts.

Transport V1 is intentionally *not* implemented here: its historical
``payload_json`` reader lives in :mod:`gemini_probe_contract_audit`.  Every new
request uses the direct V2 schema in this module.  V2 contains observable
document facts only and has no accounting, readiness, export, benchmark,
learning, or rule authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .gemini_supplementary_verification import (
    GeminiSupplementaryObservation,
    IdentityCandidateType,
    SupplementaryFailureStage,
    SupplementaryInternalObservationStatus,
    SupplementaryResolutionKind,
    SupplementarySafeDiagnostics,
    SupplementaryStageStatus,
    SupplementaryTarget,
    SupplementaryTargetType,
    SupplementaryVerificationError,
    SupplementaryVisibilityStatus,
    allowed_supplementary_resolutions,
    parse_decoded_supplementary_payload,
    validate_supplementary_observation,
)
from .supplementary_crop_framing import (
    AuthorizedCropDescriptor,
    packet_specific_schema_binding_sha256,
)


SUPPLEMENTARY_TRANSPORT_V1_VERSION = "supplementary-transport/1.x"
SUPPLEMENTARY_TRANSPORT_V2_VERSION = "supplementary-transport/2.0"


class TransportV2Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SupplementaryFinancialComponentType(str, Enum):
    SUBTOTAL = "subtotal"
    TAX = "tax"
    FEES = "fees"
    CREDITS = "credits"
    DISCOUNTS = "discounts"
    PREVIOUS_BALANCE = "previous_balance"
    PAYMENTS = "payments"
    DEPOSITS = "deposits"
    CURRENT_CHARGES = "current_charges"
    AMOUNT_DUE = "amount_due"
    LINE_ITEM_SUM = "line_item_sum"
    TOTAL_LABEL = "total_label"
    PAGE_CONTINUATION_STATUS = "page_continuation_status"


class SupplementaryEvidenceKind(str, Enum):
    PRIMARY_OBSERVATION = "primary_observation"
    IDENTITY_CANDIDATE = "identity_candidate"
    FINANCIAL_COMPONENT = "financial_component"
    LINE_ITEM = "line_item"
    STATUS_MARKER = "status_marker"
    VISIBLE_LABEL = "visible_label"
    CONTRADICTION = "contradiction"


class SupplementaryContradictionKind(str, Enum):
    OBSERVED_VALUE = "observed_value"
    IDENTITY_CANDIDATE = "identity_candidate"
    FINANCIAL_COMPONENT = "financial_component"
    VISIBLE_LABEL = "visible_label"


class GeminiSupplementaryEvidenceReferenceV2(TransportV2Model):
    """Provider-visible linkage only; provenance is enriched locally."""

    crop_id: str
    evidence_kind: SupplementaryEvidenceKind


class GeminiSupplementaryTextObservationV2(TransportV2Model):
    value: str | None
    evidence_refs: list[GeminiSupplementaryEvidenceReferenceV2]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    visibility_status: SupplementaryVisibilityStatus


class GeminiSupplementaryObservedValueV2(TransportV2Model):
    """One bounded primary candidate, with optional flat line-item fields."""

    resolution_kind: SupplementaryResolutionKind
    field_name: str | None
    value: str | int | float | Decimal | None
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
    evidence_refs: list[GeminiSupplementaryEvidenceReferenceV2]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    visibility_status: SupplementaryVisibilityStatus


class GeminiSupplementaryCandidateV2(TransportV2Model):
    value: str | None
    adjacent_label: str | None
    candidate_type: IdentityCandidateType
    evidence_refs: list[GeminiSupplementaryEvidenceReferenceV2]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    visibility_status: SupplementaryVisibilityStatus


class GeminiSupplementaryFinancialComponentV2(TransportV2Model):
    component_type: SupplementaryFinancialComponentType
    raw_value: str | int | float | Decimal | None
    evidence_refs: list[GeminiSupplementaryEvidenceReferenceV2]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    visibility_status: SupplementaryVisibilityStatus


class GeminiSupplementaryVisibleLabelV2(TransportV2Model):
    value: str | None
    evidence_refs: list[GeminiSupplementaryEvidenceReferenceV2]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    visibility_status: SupplementaryVisibilityStatus


class GeminiSupplementaryContradictionV2(TransportV2Model):
    value: str | None
    observation_kind: SupplementaryContradictionKind
    evidence_refs: list[GeminiSupplementaryEvidenceReferenceV2]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    visibility_status: SupplementaryVisibilityStatus


class GeminiSupplementaryTransportV2(TransportV2Model):
    contract_version: Literal["supplementary-transport/2.0"]
    target_type: SupplementaryTargetType
    visibility_status: SupplementaryVisibilityStatus
    unresolved_flag: bool
    contradiction_flag: bool
    page_number: int | None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    raw_visible_text: GeminiSupplementaryTextObservationV2
    observed_candidate_value: GeminiSupplementaryObservedValueV2
    observed_candidates: list[GeminiSupplementaryCandidateV2]
    financial_components: list[GeminiSupplementaryFinancialComponentV2]
    visible_labels: list[GeminiSupplementaryVisibleLabelV2]
    contradiction_observations: list[GeminiSupplementaryContradictionV2]
    warnings: list[str]


@dataclass(frozen=True)
class ParsedSupplementaryTransportV2:
    """Raw transport and normalized values are retained only in process memory."""

    raw_transport: Mapping[str, Any] = field(repr=False)
    normalized_transport: Mapping[str, Any] = field(repr=False)
    transport: GeminiSupplementaryTransportV2 = field(repr=False)
    observation: GeminiSupplementaryObservation = field(repr=False)
    diagnostics: SupplementarySafeDiagnostics


_TOP_LEVEL_FIELDS = frozenset(GeminiSupplementaryTransportV2.model_fields)
_OBSERVED_VALUE_FIELDS = frozenset(GeminiSupplementaryObservedValueV2.model_fields)
_CANDIDATE_FIELDS = frozenset(GeminiSupplementaryCandidateV2.model_fields)
_COMPONENT_FIELDS = frozenset(GeminiSupplementaryFinancialComponentV2.model_fields)
_EVIDENCE_FIELDS = frozenset(GeminiSupplementaryEvidenceReferenceV2.model_fields)
_TEXT_OBSERVATION_FIELDS = frozenset(GeminiSupplementaryTextObservationV2.model_fields)
_VISIBLE_LABEL_FIELDS = frozenset(GeminiSupplementaryVisibleLabelV2.model_fields)
_CONTRADICTION_FIELDS = frozenset(GeminiSupplementaryContradictionV2.model_fields)
_ALIASES: Mapping[str, str] = {
    "contractVersion": "contract_version",
    "targetType": "target_type",
    "visibilityStatus": "visibility_status",
    "unresolvedFlag": "unresolved_flag",
    "contradictionFlag": "contradiction_flag",
    "pageNumber": "page_number",
    "rawVisibleText": "raw_visible_text",
    "observedCandidateValue": "observed_candidate_value",
    "observedCandidates": "observed_candidates",
    "financialComponents": "financial_components",
    "evidenceRefs": "evidence_refs",
    "visibleLabels": "visible_labels",
    "contradictionObservations": "contradiction_observations",
    "observationKind": "observation_kind",
    "resolutionKind": "resolution_kind",
    "fieldName": "field_name",
    "sourcePage": "source_page",
    "sectionHeader": "section_header",
    "rowLabel": "row_label",
    "locationCandidate": "location_candidate",
    "rawDescription": "raw_description",
    "unitPrice": "unit_price",
    "cropId": "crop_id",
    "adjacentLabel": "adjacent_label",
    "candidateType": "candidate_type",
    "componentType": "component_type",
    "rawValue": "raw_value",
    "evidenceKind": "evidence_kind",
}
_INTEGER_FIELDS = frozenset({"page_number", "source_page"})
_NUMBER_FIELDS = frozenset({"confidence", "quantity", "unit_price", "amount", "tax"})
_NULLABLE_TEXT_FIELDS = frozenset({
    "field_name", "value", "section_header", "row_label",
    "location_candidate", "activity", "raw_description", "crop_id",
    "adjacent_label", "raw_value",
})
_ENUM_FIELDS: Mapping[str, type[Enum]] = {
    "target_type": SupplementaryTargetType,
    "visibility_status": SupplementaryVisibilityStatus,
    "resolution_kind": SupplementaryResolutionKind,
    "candidate_type": IdentityCandidateType,
    "component_type": SupplementaryFinancialComponentType,
    "evidence_kind": SupplementaryEvidenceKind,
    "observation_kind": SupplementaryContradictionKind,
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _field_hash(value: Any) -> str:
    return _sha256(str(value).encode("utf-8", errors="replace"))


def _safe_type(value: Any) -> str:
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


def _known_fields_for_path(path: tuple[str, ...]) -> frozenset[str]:
    if not path:
        return _TOP_LEVEL_FIELDS
    parent = next((part for part in reversed(path) if part != "[]"), "")
    if parent == "observed_candidate_value":
        return _OBSERVED_VALUE_FIELDS
    if parent == "observed_candidates":
        return _CANDIDATE_FIELDS
    if parent == "financial_components":
        return _COMPONENT_FIELDS
    if parent == "evidence_refs":
        return _EVIDENCE_FIELDS
    if parent == "raw_visible_text":
        return _TEXT_OBSERVATION_FIELDS
    if parent == "visible_labels":
        return _VISIBLE_LABEL_FIELDS
    if parent == "contradiction_observations":
        return _CONTRADICTION_FIELDS
    return frozenset()


def _normalize_enum(field_name: str, value: Any) -> tuple[Any, bool]:
    enum_type = _ENUM_FIELDS.get(field_name)
    if enum_type is None or not isinstance(value, str):
        return value, False
    comparable = value.strip().casefold().replace("-", "_").replace(" ", "_")
    matches = [item.value for item in enum_type if str(item.value).casefold() == comparable]
    if len(matches) != 1:
        return value, False
    return matches[0], matches[0] != value


def _normalize_number(field_name: str, value: Any) -> tuple[Any, bool]:
    if not isinstance(value, str):
        return value, False
    stripped = value.strip()
    if not stripped:
        return None, True
    if field_name in _INTEGER_FIELDS:
        try:
            return int(stripped), True
        except ValueError:
            return value, False
    try:
        parsed = Decimal(stripped.replace(",", ""))
    except InvalidOperation:
        return value, False
    if not parsed.is_finite():
        return value, False
    if field_name == "confidence":
        return float(parsed), True
    return parsed, True


def _normalize_node(
    value: Any,
    *,
    path: tuple[str, ...],
    actions: list[str],
    unexpected_hashes: set[str],
) -> Any:
    if isinstance(value, Mapping):
        allowed = _known_fields_for_path(path)
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            raw_name = str(raw_key)
            key = _ALIASES.get(raw_name, raw_name)
            if key not in allowed:
                unexpected_hashes.add(_field_hash(raw_name))
            if key in result:
                raise SupplementaryVerificationError(
                    "supplementary_internal_contract_invalid",
                )
            if key != raw_name:
                actions.append(f"alias:{key}")
            result[key] = _normalize_node(
                child,
                path=(*path, key),
                actions=actions,
                unexpected_hashes=unexpected_hashes,
            )
        return result
    if isinstance(value, list):
        return [
            _normalize_node(
                child,
                path=(*path, "[]"),
                actions=actions,
                unexpected_hashes=unexpected_hashes,
            )
            for child in value
        ]
    field_name = next((part for part in reversed(path) if part != "[]"), "")
    if isinstance(value, str) and not value.strip() and field_name in _NULLABLE_TEXT_FIELDS:
        actions.append(f"blank_to_null:{field_name}")
        return None
    if field_name in _INTEGER_FIELDS or field_name in _NUMBER_FIELDS:
        normalized, changed = _normalize_number(field_name, value)
        if changed:
            actions.append(
                f"integer_string:{field_name}"
                if field_name in _INTEGER_FIELDS
                else f"numeric_string:{field_name}"
            )
        return normalized
    normalized, changed = _normalize_enum(field_name, value)
    if changed:
        actions.append(f"enum:{field_name}")
    return normalized


def _base_diagnostics(
    raw_bytes: bytes,
    *,
    payload: Mapping[str, Any] | None,
    parse_result: str,
) -> SupplementarySafeDiagnostics:
    known_keys = tuple(sorted(
        _ALIASES.get(str(key), str(key))
        for key in (payload or {})
        if _ALIASES.get(str(key), str(key)) in _TOP_LEVEL_FIELDS
    ))
    return SupplementarySafeDiagnostics(
        stage=SupplementaryFailureStage.ENVELOPE,
        payload_present=True,
        payload_byte_length=len(raw_bytes),
        payload_sha256=_sha256(raw_bytes),
        payload_parse_result=parse_result,
        decoding_count=1 if payload is not None else 0,
        known_top_level_keys=known_keys,
        top_level_value_types={
            _ALIASES.get(str(key), str(key)): _safe_type(value)
            for key, value in (payload or {}).items()
            if _ALIASES.get(str(key), str(key)) in _TOP_LEVEL_FIELDS
        },
    )


def _safe_validation_code(exc: ValidationError) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    paths: set[str] = set()
    missing: set[str] = set()
    has_enum = False
    has_extra = False
    has_type = False
    for error in exc.errors(include_input=False, include_url=False):
        location = error.get("loc") or ()
        parts = ["[]" if isinstance(part, int) else str(part) for part in location]
        if parts:
            paths.add(".".join(parts))
        kind = str(error.get("type") or "")
        if kind == "missing" and parts:
            missing.add(parts[-1])
        has_enum = has_enum or kind in {"enum", "literal_error"}
        has_extra = has_extra or kind == "extra_forbidden"
        has_type = has_type or "type" in kind or kind.endswith("_parsing")
    if missing:
        code = "supplementary_required_field_missing"
    elif has_extra:
        code = "supplementary_unexpected_field"
    elif has_enum:
        code = "supplementary_enum_invalid"
    elif has_type:
        code = "supplementary_field_type_invalid"
    else:
        code = "supplementary_internal_contract_invalid"
    return code, tuple(sorted(paths)), tuple(sorted(missing))


def normalize_supplementary_transport_v2(
    payload: Mapping[str, Any],
    *,
    raw_bytes: bytes | None = None,
) -> tuple[Mapping[str, Any], SupplementarySafeDiagnostics]:
    """Apply only lossless representation normalization to direct V2 fields."""

    source_bytes = raw_bytes if raw_bytes is not None else _canonical_bytes(payload)
    diagnostics = _base_diagnostics(
        source_bytes, payload=payload, parse_result="direct_object_decoded",
    )
    actions: list[str] = []
    unexpected_hashes: set[str] = set()
    try:
        normalized = _normalize_node(
            payload, path=(), actions=actions, unexpected_hashes=unexpected_hashes,
        )
    except SupplementaryVerificationError as exc:
        failed = diagnostics.model_copy(update={
            "stage": SupplementaryFailureStage.NORMALIZATION,
            "failure_code": exc.failure_code,
            "transport_normalization_status": SupplementaryStageStatus.FAILED,
        })
        raise SupplementaryVerificationError(
            exc.failure_code, diagnostics=failed,
        ) from exc
    diagnostics = diagnostics.model_copy(update={
        "stage": SupplementaryFailureStage.NORMALIZATION,
        "normalization_actions": tuple(sorted(set(actions))),
        "unexpected_field_name_hashes": tuple(sorted(unexpected_hashes)),
        "transport_normalization_status": SupplementaryStageStatus.PASSED,
    })
    if unexpected_hashes:
        failed = diagnostics.model_copy(update={
            "failure_code": "supplementary_unexpected_field",
            "transport_validation_status": SupplementaryStageStatus.FAILED,
        })
        raise SupplementaryVerificationError(
            "supplementary_unexpected_field", diagnostics=failed,
        )
    return normalized, diagnostics


def _enriched_evidence_reference(
    reference: GeminiSupplementaryEvidenceReferenceV2,
    *,
    planned_crops: Mapping[str, Mapping[str, Any]],
    plan_id: str | None,
    packet_sha256: str | None,
) -> dict[str, Any]:
    """Attach immutable local provenance after the provider linkage validates."""

    planned = planned_crops[reference.crop_id]
    return {
        "page_number": planned.get("page_number"),
        "bbox": None,
        "crop_id": reference.crop_id,
        "crop_role": str(planned.get("role") or ""),
        "plan_id": plan_id or planned.get("plan_id"),
        "packet_sha256": packet_sha256 or planned.get("packet_sha256"),
        "source_kind": str(
            planned.get("source_kind") or "supplementary_planned_crop"
        ),
        "evidence_kind": reference.evidence_kind.value,
    }


def _enriched_evidence_references(
    references: Sequence[GeminiSupplementaryEvidenceReferenceV2],
    *,
    planned_crops: Mapping[str, Mapping[str, Any]],
    plan_id: str | None,
    packet_sha256: str | None,
) -> list[dict[str, Any]]:
    return [
        _enriched_evidence_reference(
            reference,
            planned_crops=planned_crops,
            plan_id=plan_id,
            packet_sha256=packet_sha256,
        )
        for reference in references
    ]


def _validate_crop_and_evidence(
    transport: GeminiSupplementaryTransportV2,
    *,
    planned_crops: Mapping[str, Mapping[str, Any]],
    diagnostics: SupplementarySafeDiagnostics,
) -> SupplementarySafeDiagnostics:
    planned_ids = set(planned_crops)
    ordinals = [
        int(value.get("ordinal"))
        for value in planned_crops.values()
        if value.get("ordinal") is not None
    ]
    if len(ordinals) != len(planned_crops) or sorted(ordinals) != list(
        range(len(planned_crops))
    ):
        raise SupplementaryVerificationError(
            "supplementary_evidence_reference_invalid",
            diagnostics=diagnostics.model_copy(update={
                "stage": SupplementaryFailureStage.CROP_REFERENCE,
                "failure_code": "supplementary_evidence_reference_invalid",
                "crop_reference_validation": "packet_crop_order_invalid",
            }),
        )
    component_types = [item.component_type for item in transport.financial_components]
    if len(set(component_types)) != len(component_types):
        raise SupplementaryVerificationError(
            "supplementary_internal_contract_invalid",
            diagnostics=diagnostics.model_copy(update={
                "stage": SupplementaryFailureStage.INTERNAL_CONTRACT,
                "failure_code": "supplementary_internal_contract_invalid",
            }),
        )
    all_reference_groups: list[
        tuple[Sequence[GeminiSupplementaryEvidenceReferenceV2], SupplementaryEvidenceKind]
    ] = [
        (transport.raw_visible_text.evidence_refs, SupplementaryEvidenceKind.VISIBLE_LABEL),
        (
            transport.observed_candidate_value.evidence_refs,
            SupplementaryEvidenceKind.LINE_ITEM
            if transport.observed_candidate_value.resolution_kind
            is SupplementaryResolutionKind.LINE_ITEM
            else SupplementaryEvidenceKind.PRIMARY_OBSERVATION,
        ),
        *(
            (item.evidence_refs, SupplementaryEvidenceKind.IDENTITY_CANDIDATE)
            for item in transport.observed_candidates
        ),
        *(
            (item.evidence_refs, SupplementaryEvidenceKind.FINANCIAL_COMPONENT)
            for item in transport.financial_components
        ),
        *(
            (item.evidence_refs, SupplementaryEvidenceKind.VISIBLE_LABEL)
            for item in transport.visible_labels
        ),
        *(
            (item.evidence_refs, SupplementaryEvidenceKind.CONTRADICTION)
            for item in transport.contradiction_observations
        ),
    ]
    referenced_ids = {
        reference.crop_id
        for references, _expected_kind in all_reference_groups
        for reference in references
    }
    unknown = referenced_ids - planned_ids
    if unknown:
        raise SupplementaryVerificationError(
            "supplementary_unplanned_crop_reference",
            diagnostics=diagnostics.model_copy(update={
                "stage": SupplementaryFailureStage.CROP_REFERENCE,
                "failure_code": "supplementary_unplanned_crop_reference",
                "crop_reference_validation": "unplanned",
            }),
        )
    for crop_id in referenced_ids:
        relevance = str(
            planned_crops[crop_id].get("target_relevance") or ""
        ).strip()
        if relevance and not relevance.startswith(f"{transport.target_type.value}:"):
            raise SupplementaryVerificationError(
                "supplementary_evidence_reference_invalid",
                diagnostics=diagnostics.model_copy(update={
                    "stage": SupplementaryFailureStage.EVIDENCE_REFERENCE,
                    "failure_code": "supplementary_evidence_reference_invalid",
                    "evidence_reference_validation": "different_target",
                }),
            )
    for references, expected_kind in all_reference_groups:
        keys = [(item.crop_id, item.evidence_kind.value) for item in references]
        if len(keys) != len(set(keys)):
            raise SupplementaryVerificationError(
                "supplementary_evidence_reference_invalid",
                diagnostics=diagnostics.model_copy(update={
                    "stage": SupplementaryFailureStage.EVIDENCE_REFERENCE,
                    "failure_code": "supplementary_evidence_reference_invalid",
                    "evidence_reference_validation": "duplicate_item_reference",
                }),
            )
        if any(item.evidence_kind is not expected_kind for item in references):
            raise SupplementaryVerificationError(
                "supplementary_evidence_reference_invalid",
                diagnostics=diagnostics.model_copy(update={
                    "stage": SupplementaryFailureStage.EVIDENCE_REFERENCE,
                    "failure_code": "supplementary_evidence_reference_invalid",
                    "evidence_reference_validation": "evidence_kind_mismatch",
                }),
            )

    def require_visible_evidence(
        *, value: Any,
        references: Sequence[GeminiSupplementaryEvidenceReferenceV2],
        visibility: SupplementaryVisibilityStatus,
    ) -> None:
        if (
            visibility is SupplementaryVisibilityStatus.VISIBLE
            and value not in (None, "")
            and not references
        ):
            raise SupplementaryVerificationError(
                "supplementary_visible_value_without_evidence",
                diagnostics=diagnostics.model_copy(update={
                    "stage": SupplementaryFailureStage.EVIDENCE_REFERENCE,
                    "failure_code": "supplementary_visible_value_without_evidence",
                    "evidence_reference_validation": "missing_for_visible_value",
                }),
            )
        if visibility is SupplementaryVisibilityStatus.AMBIGUOUS and not references:
            raise SupplementaryVerificationError(
                "supplementary_ambiguous_value_without_evidence",
                diagnostics=diagnostics.model_copy(update={
                    "stage": SupplementaryFailureStage.EVIDENCE_REFERENCE,
                    "failure_code": "supplementary_ambiguous_value_without_evidence",
                    "evidence_reference_validation": "missing_for_ambiguous_value",
                }),
            )
        if visibility is SupplementaryVisibilityStatus.NOT_VISIBLE and value not in (None, ""):
            raise SupplementaryVerificationError(
                "supplementary_internal_contract_invalid",
                diagnostics=diagnostics.model_copy(update={
                    "stage": SupplementaryFailureStage.INTERNAL_CONTRACT,
                    "failure_code": "supplementary_internal_contract_invalid",
                }),
            )

    primary = transport.observed_candidate_value
    primary_value = primary.value
    if primary.resolution_kind is SupplementaryResolutionKind.LINE_ITEM:
        primary_value = next((
            value for value in (
                primary.raw_description, primary.activity, primary.amount,
                primary.quantity, primary.unit_price,
            ) if value not in (None, "")
        ), None)
    require_visible_evidence(
        value=primary_value,
        references=primary.evidence_refs,
        visibility=primary.visibility_status,
    )
    require_visible_evidence(
        value=transport.raw_visible_text.value,
        references=transport.raw_visible_text.evidence_refs,
        visibility=transport.raw_visible_text.visibility_status,
    )
    for candidate in transport.observed_candidates:
        require_visible_evidence(
            value=candidate.value,
            references=candidate.evidence_refs,
            visibility=candidate.visibility_status,
        )
    for component in transport.financial_components:
        require_visible_evidence(
            value=component.raw_value,
            references=component.evidence_refs,
            visibility=component.visibility_status,
        )
    for label in transport.visible_labels:
        require_visible_evidence(
            value=label.value,
            references=label.evidence_refs,
            visibility=label.visibility_status,
        )
    for contradiction in transport.contradiction_observations:
        require_visible_evidence(
            value=contradiction.value,
            references=contradiction.evidence_refs,
            visibility=contradiction.visibility_status,
        )
    if (
        transport.visibility_status is SupplementaryVisibilityStatus.VISIBLE
        and not referenced_ids
    ):
        raise SupplementaryVerificationError(
            "supplementary_evidence_reference_missing",
            diagnostics=diagnostics.model_copy(update={
                "stage": SupplementaryFailureStage.EVIDENCE_REFERENCE,
                "failure_code": "supplementary_evidence_reference_missing",
                "evidence_reference_validation": "missing_for_visible_observation",
            }),
        )
    if (
        transport.visibility_status is SupplementaryVisibilityStatus.AMBIGUOUS
        and not referenced_ids
    ):
        raise SupplementaryVerificationError(
            "supplementary_ambiguous_value_without_evidence",
            diagnostics=diagnostics.model_copy(update={
                "stage": SupplementaryFailureStage.EVIDENCE_REFERENCE,
                "failure_code": "supplementary_ambiguous_value_without_evidence",
                "evidence_reference_validation": "missing_for_ambiguous_observation",
            }),
        )
    if transport.contradiction_flag and len(transport.contradiction_observations) < 2:
        raise SupplementaryVerificationError(
            "supplementary_internal_contract_invalid",
            diagnostics=diagnostics.model_copy(update={
                "stage": SupplementaryFailureStage.INTERNAL_CONTRACT,
                "failure_code": "supplementary_internal_contract_invalid",
            }),
        )
    if (
        transport.visibility_status is SupplementaryVisibilityStatus.NOT_VISIBLE
        and not transport.unresolved_flag
    ):
        raise SupplementaryVerificationError(
            "supplementary_internal_contract_invalid",
            diagnostics=diagnostics.model_copy(update={
                "stage": SupplementaryFailureStage.INTERNAL_CONTRACT,
                "failure_code": "supplementary_internal_contract_invalid",
            }),
        )
    return diagnostics.model_copy(update={
        "evidence_reference_validation": "valid",
        "crop_reference_validation": "valid",
    })


def _internal_payload(
    transport: GeminiSupplementaryTransportV2,
    *,
    planned_crops: Mapping[str, Mapping[str, Any]],
    plan_id: str | None,
    packet_sha256: str | None,
) -> dict[str, Any]:
    primary = transport.observed_candidate_value
    primary_references = _enriched_evidence_references(
        primary.evidence_refs,
        planned_crops=planned_crops,
        plan_id=plan_id,
        packet_sha256=packet_sha256,
    )
    observed_value = None
    if primary.resolution_kind is not SupplementaryResolutionKind.NONE:
        line_item = None
        if primary.resolution_kind is SupplementaryResolutionKind.LINE_ITEM:
            line_item = {
                "source_page": primary.source_page,
                "section_header": primary.section_header,
                "row_label": primary.row_label,
                "location_candidate": primary.location_candidate,
                "activity": primary.activity,
                "raw_description": primary.raw_description,
                "quantity": primary.quantity,
                "unit_price": primary.unit_price,
                "amount": primary.amount,
                "tax": primary.tax,
            }
        observed_value = {
            "resolution_kind": primary.resolution_kind.value,
            "field_name": primary.field_name,
            "raw_value": primary.value,
            "line_item": line_item,
            "evidence_references": primary_references,
        }

    observed_candidates: list[dict[str, Any]] = []
    for candidate in transport.observed_candidates:
        references = _enriched_evidence_references(
            candidate.evidence_refs,
            planned_crops=planned_crops,
            plan_id=plan_id,
            packet_sha256=packet_sha256,
        )
        observed_candidates.append({
            "raw_candidate": candidate.value,
            "adjacent_visible_label": candidate.adjacent_label,
            "candidate_type": candidate.candidate_type.value,
            "evidence_reference": references[0] if references else None,
            "evidence_references": references,
            "confidence": candidate.confidence,
            "unresolved": (
                candidate.visibility_status is not SupplementaryVisibilityStatus.VISIBLE
                or candidate.value in (None, "")
            ),
        })

    financial = None
    if transport.financial_components:
        financial = {
            "subtotal": None,
            "tax": None,
            "fees": None,
            "credits": None,
            "discounts": None,
            "previous_balance": None,
            "payments": None,
            "deposits": None,
            "current_charges": None,
            "amount_due": None,
            "line_item_sum": None,
            "total_label": None,
            "page_continuation_status": None,
            "evidence_references": [],
            "component_evidence_references": {},
        }
        for component in transport.financial_components:
            financial[component.component_type.value] = component.raw_value
            references = _enriched_evidence_references(
                component.evidence_refs,
                planned_crops=planned_crops,
                plan_id=plan_id,
                packet_sha256=packet_sha256,
            )
            financial["component_evidence_references"][
                component.component_type.value
            ] = references
            known = {
                (item["crop_id"], item["evidence_kind"])
                for item in financial["evidence_references"]
            }
            for reference in references:
                key = (reference["crop_id"], reference["evidence_kind"])
                if key not in known:
                    financial["evidence_references"].append(reference)
                    known.add(key)

    raw_text_references = _enriched_evidence_references(
        transport.raw_visible_text.evidence_refs,
        planned_crops=planned_crops,
        plan_id=plan_id,
        packet_sha256=packet_sha256,
    )
    visible_labels = [
        {
            "raw_label": item.value,
            "visibility_status": item.visibility_status.value,
            "confidence": item.confidence,
            "evidence_references": _enriched_evidence_references(
                item.evidence_refs,
                planned_crops=planned_crops,
                plan_id=plan_id,
                packet_sha256=packet_sha256,
            ),
        }
        for item in transport.visible_labels
    ]
    contradiction_observations = [
        {
            "raw_candidate": item.value,
            "observation_kind": item.observation_kind.value,
            "visibility_status": item.visibility_status.value,
            "confidence": item.confidence,
            "evidence_references": _enriched_evidence_references(
                item.evidence_refs,
                planned_crops=planned_crops,
                plan_id=plan_id,
                packet_sha256=packet_sha256,
            ),
        }
        for item in transport.contradiction_observations
    ]

    return {
        "target_type": transport.target_type.value,
        "observed_candidate_value": observed_value,
        "raw_visible_text": transport.raw_visible_text.value,
        "page_number": transport.page_number,
        "evidence_reference": primary_references[0] if primary_references else None,
        "confidence": transport.confidence,
        "contradiction_flag": transport.contradiction_flag,
        "unresolved_flag": transport.unresolved_flag,
        "warnings": list(transport.warnings),
        "visibility_status": transport.visibility_status.value,
        "observed_candidates": observed_candidates,
        "financial_components": financial,
        "raw_visible_text_evidence_references": raw_text_references,
        "visible_labels": visible_labels,
        "contradiction_observations": contradiction_observations,
    }


def parse_supplementary_transport_v2_response_with_audit(
    raw_response: str,
    *,
    target: SupplementaryTarget,
    planned_crops: Mapping[str, Mapping[str, Any]],
    plan_id: str | None = None,
    packet_sha256: str | None = None,
) -> ParsedSupplementaryTransportV2:
    """Parse one direct JSON object; never unwrap or repeatedly decode it."""

    raw_bytes = str(raw_response).encode("utf-8")
    try:
        payload = json.loads(raw_response)
    except (TypeError, json.JSONDecodeError) as exc:
        diagnostics = _base_diagnostics(
            raw_bytes, payload=None, parse_result="direct_json_invalid",
        ).model_copy(update={
            "failure_code": "supplementary_field_type_invalid",
            "transport_validation_status": SupplementaryStageStatus.FAILED,
        })
        raise SupplementaryVerificationError(
            "supplementary_field_type_invalid", diagnostics=diagnostics,
        ) from exc
    if not isinstance(payload, Mapping):
        diagnostics = _base_diagnostics(
            raw_bytes, payload=None, parse_result="direct_value_not_object",
        ).model_copy(update={
            "failure_code": "supplementary_field_type_invalid",
            "transport_validation_status": SupplementaryStageStatus.FAILED,
        })
        raise SupplementaryVerificationError(
            "supplementary_field_type_invalid", diagnostics=diagnostics,
        )
    normalized, diagnostics = normalize_supplementary_transport_v2(
        payload, raw_bytes=raw_bytes,
    )
    version = normalized.get("contract_version")
    if version is not None and version != SUPPLEMENTARY_TRANSPORT_V2_VERSION:
        failed = diagnostics.model_copy(update={
            "stage": SupplementaryFailureStage.INTERNAL_CONTRACT,
            "failure_code": "supplementary_transport_version_invalid",
            "transport_validation_status": SupplementaryStageStatus.FAILED,
        })
        raise SupplementaryVerificationError(
            "supplementary_transport_version_invalid", diagnostics=failed,
        )
    try:
        transport = GeminiSupplementaryTransportV2.model_validate(normalized)
    except ValidationError as exc:
        code, paths, missing = _safe_validation_code(exc)
        failed = diagnostics.model_copy(update={
            "stage": SupplementaryFailureStage.INTERNAL_CONTRACT,
            "failure_code": code,
            "internal_validation_paths": paths,
            "missing_required_fields": missing,
            "transport_validation_status": SupplementaryStageStatus.FAILED,
        })
        raise SupplementaryVerificationError(code, diagnostics=failed) from exc
    if transport.target_type is not target.target_type:
        failed = diagnostics.model_copy(update={
            "stage": SupplementaryFailureStage.INTERNAL_CONTRACT,
            "failure_code": "supplementary_enum_invalid",
            "invalid_enum_categories": ("SupplementaryTargetType",),
            "transport_validation_status": SupplementaryStageStatus.FAILED,
        })
        raise SupplementaryVerificationError(
            "supplementary_enum_invalid", diagnostics=failed,
        )
    diagnostics = diagnostics.model_copy(update={
        "transport_validation_status": SupplementaryStageStatus.PASSED,
    })
    try:
        diagnostics = _validate_crop_and_evidence(
            transport, planned_crops=planned_crops, diagnostics=diagnostics,
        )
    except SupplementaryVerificationError as exc:
        failed = (exc.diagnostics or diagnostics).model_copy(update={
            "transport_validation_status": SupplementaryStageStatus.PASSED,
            "evidence_validation_status": SupplementaryStageStatus.FAILED,
            "internal_observation_status": (
                SupplementaryInternalObservationStatus.NOT_CONSTRUCTED
            ),
        })
        raise SupplementaryVerificationError(
            exc.failure_code, diagnostics=failed,
        ) from exc
    diagnostics = diagnostics.model_copy(update={
        "evidence_validation_status": SupplementaryStageStatus.PASSED,
    })
    internal_payload = _internal_payload(
        transport,
        planned_crops=planned_crops,
        plan_id=plan_id,
        packet_sha256=packet_sha256,
    )
    try:
        observation, internal_diagnostics = parse_decoded_supplementary_payload(
            internal_payload, target=target,
        )
        observation = validate_supplementary_observation(
            observation, target=target, diagnostics=internal_diagnostics,
        )
    except SupplementaryVerificationError as exc:
        failed = (exc.diagnostics or diagnostics).model_copy(update={
            "payload_present": diagnostics.payload_present,
            "payload_byte_length": diagnostics.payload_byte_length,
            "payload_sha256": diagnostics.payload_sha256,
            "payload_parse_result": diagnostics.payload_parse_result,
            "decoding_count": diagnostics.decoding_count,
            "normalization_actions": diagnostics.normalization_actions,
            "crop_reference_validation": diagnostics.crop_reference_validation,
            "evidence_reference_validation": diagnostics.evidence_reference_validation,
            "transport_validation_status": SupplementaryStageStatus.PASSED,
            "transport_normalization_status": SupplementaryStageStatus.PASSED,
            "evidence_validation_status": SupplementaryStageStatus.PASSED,
            "internal_observation_status": SupplementaryInternalObservationStatus.FAILED,
        })
        raise SupplementaryVerificationError(
            exc.failure_code, diagnostics=failed,
        ) from exc
    diagnostics = diagnostics.model_copy(update={
        "stage": SupplementaryFailureStage.INTERNAL_CONTRACT,
        "failure_code": None,
        "transport_validation_status": SupplementaryStageStatus.PASSED,
        "transport_normalization_status": SupplementaryStageStatus.PASSED,
        "evidence_validation_status": SupplementaryStageStatus.PASSED,
        "internal_observation_status": SupplementaryInternalObservationStatus.CONSTRUCTED,
    })
    return ParsedSupplementaryTransportV2(
        raw_transport=copy.deepcopy(dict(payload)),
        normalized_transport=copy.deepcopy(dict(normalized)),
        transport=transport,
        observation=observation,
        diagnostics=diagnostics,
    )


def parse_supplementary_transport_v2_response(
    raw_response: str,
    *,
    target: SupplementaryTarget,
    planned_crops: Mapping[str, Mapping[str, Any]],
    plan_id: str | None = None,
    packet_sha256: str | None = None,
) -> GeminiSupplementaryObservation:
    return parse_supplementary_transport_v2_response_with_audit(
        raw_response,
        target=target,
        planned_crops=planned_crops,
        plan_id=plan_id,
        packet_sha256=packet_sha256,
    ).observation


def _nullable(schema_type: str) -> dict[str, Any]:
    return {"type": [schema_type, "null"]}


def supplementary_transport_v2_response_format(
    target: SupplementaryTarget,
    *,
    planned_crops: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a direct schema bound to the packet's exact ordered crop IDs."""

    ordered = sorted(
        (
            int(value.get("ordinal")),
            str(crop_id),
            str(value.get("role") or "").strip(),
        )
        for crop_id, value in planned_crops.items()
        if value.get("ordinal") is not None
    )
    crop_ids = [item[1] for item in ordered]
    if (
        not crop_ids
        or len(ordered) != len(planned_crops)
        or [item[0] for item in ordered] != list(range(len(planned_crops)))
        or any(not item[2] for item in ordered)
        or len(set(crop_ids)) != len(crop_ids)
    ):
        raise SupplementaryVerificationError(
            "supplementary_evidence_reference_invalid",
        )
    nullable_string = _nullable("string")
    nullable_number = {"type": ["number", "string", "null"]}
    nullable_integer = {"type": ["integer", "string", "null"]}
    evidence = {
        "type": "object",
        "properties": {
            "crop_id": {"type": "string", "enum": crop_ids},
            "evidence_kind": {
                "type": "string",
                "enum": [item.value for item in SupplementaryEvidenceKind],
            },
        },
        "required": list(GeminiSupplementaryEvidenceReferenceV2.model_fields),
        "additionalProperties": False,
    }
    evidence_refs = {"type": "array", "items": evidence}
    visibility = {
        "type": "string",
        "enum": [item.value for item in SupplementaryVisibilityStatus],
    }
    text_observation = {
        "type": "object",
        "properties": {
            "value": nullable_string,
            "evidence_refs": evidence_refs,
            "confidence": _nullable("number"),
            "visibility_status": visibility,
        },
        "required": list(GeminiSupplementaryTextObservationV2.model_fields),
        "additionalProperties": False,
    }
    observed_value = {
        "type": "object",
        "properties": {
            "resolution_kind": {
                "type": "string",
                "enum": sorted(
                    item.value
                    for item in allowed_supplementary_resolutions(target.target_type)
                ),
            },
            "field_name": nullable_string,
            "value": nullable_number,
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
            "evidence_refs": evidence_refs,
            "confidence": _nullable("number"),
            "visibility_status": visibility,
        },
        "required": list(GeminiSupplementaryObservedValueV2.model_fields),
        "additionalProperties": False,
    }
    candidate = {
        "type": "object",
        "properties": {
            "value": nullable_string,
            "adjacent_label": nullable_string,
            "observation_kind": {
                "type": "string",
                "enum": [item.value for item in SupplementaryContradictionKind],
            },
            "evidence_refs": evidence_refs,
            "confidence": _nullable("number"),
            "visibility_status": visibility,
        },
        "required": list(GeminiSupplementaryCandidateV2.model_fields),
        "additionalProperties": False,
    }
    component = {
        "type": "object",
        "properties": {
            "component_type": {
                "type": "string",
                "enum": [item.value for item in SupplementaryFinancialComponentType],
            },
            "raw_value": nullable_number,
            "evidence_refs": evidence_refs,
            "confidence": _nullable("number"),
            "visibility_status": visibility,
        },
        "required": list(GeminiSupplementaryFinancialComponentV2.model_fields),
        "additionalProperties": False,
    }
    visible_label = {
        "type": "object",
        "properties": {
            "value": nullable_string,
            "evidence_refs": evidence_refs,
            "confidence": _nullable("number"),
            "visibility_status": visibility,
        },
        "required": list(GeminiSupplementaryVisibleLabelV2.model_fields),
        "additionalProperties": False,
    }
    contradiction = {
        "type": "object",
        "properties": {
            "value": nullable_string,
            "candidate_type": {
                "type": "string", "enum": [item.value for item in IdentityCandidateType],
            },
            "evidence_refs": evidence_refs,
            "confidence": _nullable("number"),
            "visibility_status": visibility,
        },
        "required": list(GeminiSupplementaryContradictionV2.model_fields),
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            "contract_version": {
                "type": "string", "enum": [SUPPLEMENTARY_TRANSPORT_V2_VERSION],
            },
            "target_type": {"type": "string", "enum": [target.target_type.value]},
            "visibility_status": visibility,
            "unresolved_flag": {"type": "boolean"},
            "contradiction_flag": {"type": "boolean"},
            "page_number": nullable_integer,
            "confidence": _nullable("number"),
            "raw_visible_text": text_observation,
            "observed_candidate_value": observed_value,
            "observed_candidates": {"type": "array", "items": candidate},
            "financial_components": {"type": "array", "items": component},
            "visible_labels": {"type": "array", "items": visible_label},
            "contradiction_observations": {
                "type": "array", "items": contradiction,
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": list(GeminiSupplementaryTransportV2.model_fields),
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": f"innerview_supplementary_transport_v2_{target.target_type.value}",
            "strict": True,
            "schema": schema,
        },
    }


def supplementary_transport_v2_schema_sha256(
    target: SupplementaryTarget,
    *,
    planned_crops: Mapping[str, Mapping[str, Any]],
) -> str:
    schema = supplementary_transport_v2_response_format(
        target, planned_crops=planned_crops,
    )["json_schema"]["schema"]
    return _sha256(_canonical_bytes(schema))


def supplementary_transport_v2_packet_schema_sha256(
    target: SupplementaryTarget,
    *,
    planned_crops: Mapping[str, Mapping[str, Any]],
    packet_sha256: str,
) -> str:
    schema = supplementary_transport_v2_response_format(
        target, planned_crops=planned_crops,
    )["json_schema"]["schema"]
    descriptors = tuple(
        AuthorizedCropDescriptor(
            crop_id=str(crop_id),
            crop_role=str(metadata.get("role") or ""),
            ordinal=int(metadata.get("ordinal")),
            target_relevance=str(
                metadata.get("target_relevance") or target.target_type.value
            ),
            mime_type=str(metadata.get("mime_type") or "image/jpeg"),
            page_number=metadata.get("page_number"),
            source_kind=str(
                metadata.get("source_kind") or "supplementary_planned_crop"
            ),
        )
        for crop_id, metadata in sorted(
            planned_crops.items(), key=lambda item: int(item[1].get("ordinal"))
        )
    )
    return packet_specific_schema_binding_sha256(
        schema=schema,
        packet_sha256=packet_sha256,
        descriptors=descriptors,
        transport_version=SUPPLEMENTARY_TRANSPORT_V2_VERSION,
    )


def supplementary_transport_v2_family_sha256() -> str:
    """Stable privacy-free fingerprint for the complete semantic V2 family."""

    schemas: dict[str, Any] = {}
    planned = {"synthetic-planned-crop": {"role": "synthetic-role", "ordinal": 0}}
    for target_type in SupplementaryTargetType:
        target = SupplementaryTarget(
            target_type=target_type,
            page_number=1,
            field_name="synthetic_field",
            local_trigger_codes=["v2_family_fingerprint"],
        )
        schemas[target_type.value] = supplementary_transport_v2_response_format(
            target, planned_crops=planned,
        )["json_schema"]["schema"]
    return _sha256(_canonical_bytes(schemas))


__all__ = [
    "GeminiSupplementaryCandidateV2",
    "GeminiSupplementaryEvidenceReferenceV2",
    "GeminiSupplementaryFinancialComponentV2",
    "GeminiSupplementaryObservedValueV2",
    "GeminiSupplementaryTextObservationV2",
    "GeminiSupplementaryTransportV2",
    "GeminiSupplementaryVisibleLabelV2",
    "GeminiSupplementaryContradictionV2",
    "ParsedSupplementaryTransportV2",
    "SUPPLEMENTARY_TRANSPORT_V1_VERSION",
    "SUPPLEMENTARY_TRANSPORT_V2_VERSION",
    "SupplementaryEvidenceKind",
    "SupplementaryContradictionKind",
    "SupplementaryFinancialComponentType",
    "normalize_supplementary_transport_v2",
    "parse_supplementary_transport_v2_response",
    "parse_supplementary_transport_v2_response_with_audit",
    "supplementary_transport_v2_response_format",
    "supplementary_transport_v2_family_sha256",
    "supplementary_transport_v2_schema_sha256",
    "supplementary_transport_v2_packet_schema_sha256",
]
