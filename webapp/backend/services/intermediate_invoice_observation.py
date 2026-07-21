"""Immutable observations between provider transport and strict DocumentFacts.

This contract deliberately has no accounting, GL, readiness, export, label,
learning, or rule authority.  It exists only so evidence-backed visual facts
that fail deterministic arithmetic reconciliation can receive one bounded,
targeted supplementary verification before being either promoted to strict
facts or left safely review-required.
"""

from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .reconciliation_observability import ReconciliationState


INTERMEDIATE_OBSERVATION_SCHEMA_VERSION = "unreconciled-invoice-observation/1.0"
NORMALIZATION_OUTCOME_VERSION = "initial-normalization-outcome/1.0"


class IntermediateModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InitialNormalizationCategory(str, Enum):
    FACTS_READY = "facts_ready"
    SUPPLEMENTARY_REQUIRED = "supplementary_required"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


class EligibleSupplementaryTarget(str, Enum):
    TOTAL_MISMATCH = "total_mismatch"


class IntermediateEvidenceReference(IntermediateModel):
    page: int | None = None
    text: str | None = None
    normalized_text: str | None = None
    bbox: tuple[float, ...] | None = None
    source_type: str
    extraction_method: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ObservableHeaderFields(IntermediateModel):
    vendor_name: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    service_date: str | None = None
    due_date: str | None = None
    due_date_text: str | None = None
    payment_terms: str | None = None
    bill_or_credit: str | None = None
    account_number: str | None = None
    service_address: str | None = None
    sold_to_raw_text: str | None = None
    job_site_raw_text: str | None = None
    address_role: str | None = None
    location_candidate: str | None = None
    property_candidate: str | None = None
    property_abbreviation: str | None = None
    invoice_description: str | None = None


class ObservableLineItem(IntermediateModel):
    source_page: int | None = None
    section_header: str | None = None
    row_label: str | None = None
    location_candidate: str | None = None
    activity: str | None = None
    raw_description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    tax: Decimal | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: tuple[IntermediateEvidenceReference, ...] = ()


class ObservableAmountComponent(IntermediateModel):
    label: str | None = None
    amount: Decimal | None = None


class ObservablePaidMarker(IntermediateModel):
    page: int | None = None
    text: str | None = None
    bbox: tuple[float, ...] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ObservableExcludedPaidRow(IntermediateModel):
    raw_apartment_number: str | None = None
    component_amounts: tuple[ObservableAmountComponent, ...] = ()
    row_total: Decimal | None = None
    paid_marker_evidence: tuple[ObservablePaidMarker, ...] = ()
    exclusion_reason: str | None = None


class ObservableFinancialCandidates(IntermediateModel):
    subtotal: Decimal | None = None
    tax_amount: Decimal | None = None
    shipping_amount: Decimal | None = None
    fees_amount: Decimal | None = None
    total_amount: Decimal | None = None


class ObservablePageReconciliation(IntermediateModel):
    page: int | None = None
    component_total: Decimal | None = None
    printed_total: Decimal | None = None
    status: str | None = None


class IntermediateObservationProvenance(IntermediateModel):
    source_role: str = "initial_visual_observation"
    provider: str
    profile_id: str
    model_id: str
    transport_schema_version: str
    transport_prompt_version: str
    normalization_stage: str = "strict_internal_reconciliation"


class UnreconciledInvoiceObservation(IntermediateModel):
    """Transport-valid observed facts that are not strict DocumentFacts."""

    schema_version: str = INTERMEDIATE_OBSERVATION_SCHEMA_VERSION
    opaque_document_id: str
    opaque_page_ids: tuple[str, ...] = ()
    header: ObservableHeaderFields
    line_items: tuple[ObservableLineItem, ...]
    excluded_paid_rows: tuple[ObservableExcludedPaidRow, ...] = ()
    financial_candidates: ObservableFinancialCandidates
    page_reconciliations: tuple[ObservablePageReconciliation, ...] = ()
    deterministic_line_item_sum: Decimal
    deterministic_adder_sum: Decimal
    deterministic_reconciliation_delta: Decimal | None
    evidence: tuple[IntermediateEvidenceReference, ...] = ()
    provenance: IntermediateObservationProvenance
    normalization_warnings: tuple[str, ...] = ()
    reconciliation_state: ReconciliationState = ReconciliationState.RAN_UNRECONCILED
    eligible_supplementary_targets: tuple[EligibleSupplementaryTarget, ...] = (
        EligibleSupplementaryTarget.TOTAL_MISMATCH,
    )
    observation_sha256: str

    def to_supplementary_payload(self) -> dict[str, Any]:
        """Return a fresh minimized-authority payload for local target selection.

        The returned mapping contains observations only.  It never contains a
        GL, readiness result, export decision, label, rule, or accepted flag.
        """

        header = self.header.model_dump(mode="python")
        payload: dict[str, Any] = {
            **header,
            "line_items": [
                {
                    **item.model_dump(mode="python", exclude={"evidence"}),
                    "description": item.raw_description,
                    "evidence": [
                        evidence.model_dump(mode="python")
                        for evidence in item.evidence
                    ],
                }
                for item in self.line_items
            ],
            "excluded_paid_rows": [
                row.model_dump(mode="python") for row in self.excluded_paid_rows
            ],
            **self.financial_candidates.model_dump(mode="python"),
            "page_reconciliations": [
                item.model_dump(mode="python") for item in self.page_reconciliations
            ],
            "evidence": [item.model_dump(mode="python") for item in self.evidence],
            "warnings": list(self.normalization_warnings),
            "needs_manual_review": True,
            "visual_extraction_status": "partial",
            "transport_schema_version": self.provenance.transport_schema_version,
            "transport_prompt_version": self.provenance.transport_prompt_version,
            "intermediate_observation_schema_version": self.schema_version,
            "initial_observation_sha256": self.observation_sha256,
            "reconciliation_state": self.reconciliation_state.value,
            "reconciliation_ran": True,
            "reconciliation_status": "unreconciled",
            "reconciliation_source_stage": "initial_strict_reconciliation",
            "reconciliation_before": "unreconciled",
            "reconciliation_after": "unreconciled",
            "reconciliation_delta_before": str(
                self.deterministic_reconciliation_delta
            ) if self.deterministic_reconciliation_delta is not None else None,
            "reconciliation_delta_after": str(
                self.deterministic_reconciliation_delta
            ) if self.deterministic_reconciliation_delta is not None else None,
            "supplementary_visual_status": "not_run",
        }
        return copy.deepcopy(payload)


class InitialNormalizationOutcome(IntermediateModel):
    contract_version: str = NORMALIZATION_OUTCOME_VERSION
    category: InitialNormalizationCategory
    validation_path: str | None = None
    failure_code: str | None = None
    facts_payload: dict[str, Any] | None = None
    working_observation_payload: dict[str, Any] | None = None
    observation: UnreconciledInvoiceObservation | None = None

    @classmethod
    def facts_ready(cls, payload: Mapping[str, Any]) -> "InitialNormalizationOutcome":
        return cls(
            category=InitialNormalizationCategory.FACTS_READY,
            facts_payload=copy.deepcopy(dict(payload)),
        )

    @classmethod
    def supplementary_required(
        cls, observation: UnreconciledInvoiceObservation,
        *, validation_path: str,
    ) -> "InitialNormalizationOutcome":
        return cls(
            category=InitialNormalizationCategory.SUPPLEMENTARY_REQUIRED,
            validation_path=validation_path,
            failure_code="supplementary_visual_evidence_unresolved",
            working_observation_payload=observation.to_supplementary_payload(),
            observation=observation,
        )

    @classmethod
    def unsupported(
        cls, *, validation_path: str, failure_code: str,
    ) -> "InitialNormalizationOutcome":
        return cls(
            category=InitialNormalizationCategory.UNSUPPORTED,
            validation_path=validation_path,
            failure_code=failure_code,
        )

    @classmethod
    def blocked(
        cls, *, validation_path: str, failure_code: str,
    ) -> "InitialNormalizationOutcome":
        return cls(
            category=InitialNormalizationCategory.BLOCKED,
            validation_path=validation_path,
            failure_code=failure_code,
        )


def build_unreconciled_observation(
    payload: Mapping[str, Any], *, opaque_document_id: str,
    provider: str, profile_id: str, model_id: str,
) -> UnreconciledInvoiceObservation:
    """Freeze one transport-normalized mismatch without inventing values."""

    rows = tuple(_line_item(item) for item in payload.get("line_items") or [] if isinstance(item, Mapping))
    line_sum = sum((_decimal(item.amount) for item in rows), Decimal("0"))
    financial = ObservableFinancialCandidates(
        subtotal=_decimal_or_none(payload.get("subtotal")),
        tax_amount=_decimal_or_none(payload.get("tax_amount")),
        shipping_amount=_decimal_or_none(payload.get("shipping_amount")),
        fees_amount=_decimal_or_none(payload.get("fees_amount")),
        total_amount=_decimal_or_none(payload.get("total_amount")),
    )
    adders = sum((
        _decimal(financial.tax_amount),
        _decimal(financial.shipping_amount),
        _decimal(financial.fees_amount),
    ), Decimal("0"))
    delta = (
        financial.total_amount - line_sum - adders
        if financial.total_amount is not None else None
    )
    header_fields = ObservableHeaderFields.model_fields
    header = ObservableHeaderFields(**{
        key: _text(payload.get(key)) for key in header_fields
    })
    pages = sorted({
        item.source_page for item in rows if item.source_page is not None
    } | {
        int(item.get("page")) for item in payload.get("page_reconciliations") or []
        if isinstance(item, Mapping) and item.get("page") is not None
    })
    stable = {
        "opaque_document_id": opaque_document_id,
        "header": header.model_dump(mode="json"),
        "line_items": [item.model_dump(mode="json") for item in rows],
        "financial_candidates": financial.model_dump(mode="json"),
        "line_item_sum": str(line_sum),
        "adder_sum": str(adders),
        "delta": str(delta) if delta is not None else None,
    }
    observation_hash = hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return UnreconciledInvoiceObservation(
        opaque_document_id=opaque_document_id,
        opaque_page_ids=tuple(
            hashlib.sha256(f"{opaque_document_id}:page:{page}".encode()).hexdigest()[:20]
            for page in pages
        ),
        header=header,
        line_items=rows,
        excluded_paid_rows=tuple(
            _paid_row(item) for item in payload.get("excluded_paid_rows") or []
            if isinstance(item, Mapping)
        ),
        financial_candidates=financial,
        page_reconciliations=tuple(
            ObservablePageReconciliation(
                page=_int_or_none(item.get("page")),
                component_total=_decimal_or_none(item.get("component_total")),
                printed_total=_decimal_or_none(item.get("printed_total")),
                status=_text(item.get("status")),
            )
            for item in payload.get("page_reconciliations") or []
            if isinstance(item, Mapping)
        ),
        deterministic_line_item_sum=line_sum,
        deterministic_adder_sum=adders,
        deterministic_reconciliation_delta=delta,
        evidence=tuple(
            _evidence(item) for item in payload.get("evidence") or []
            if isinstance(item, Mapping)
        ),
        provenance=IntermediateObservationProvenance(
            provider=provider,
            profile_id=profile_id,
            model_id=model_id,
            transport_schema_version=_text(payload.get("transport_schema_version")) or "unknown",
            transport_prompt_version=_text(payload.get("transport_prompt_version")) or "unknown",
        ),
        normalization_warnings=tuple(dict.fromkeys(
            str(item) for item in payload.get("warnings") or [] if str(item).strip()
        )),
        observation_sha256=observation_hash,
    )


def _line_item(item: Mapping[str, Any]) -> ObservableLineItem:
    return ObservableLineItem(
        source_page=_int_or_none(item.get("source_page")),
        section_header=_text(item.get("section_header")),
        row_label=_text(item.get("row_label")),
        location_candidate=_text(item.get("location_candidate")),
        activity=_text(item.get("activity")),
        raw_description=_text(item.get("raw_description") or item.get("description")),
        quantity=_decimal_or_none(item.get("quantity")),
        unit_price=_decimal_or_none(item.get("unit_price")),
        amount=_decimal_or_none(item.get("amount")),
        tax=_decimal_or_none(item.get("tax")),
        confidence=_confidence(item.get("confidence")),
        evidence=tuple(
            _evidence(value) for value in item.get("evidence") or []
            if isinstance(value, Mapping)
        ),
    )


def _paid_row(item: Mapping[str, Any]) -> ObservableExcludedPaidRow:
    return ObservableExcludedPaidRow(
        raw_apartment_number=_text(item.get("raw_apartment_number")),
        component_amounts=tuple(
            ObservableAmountComponent(
                label=_text(value.get("label")),
                amount=_decimal_or_none(value.get("amount")),
            )
            for value in item.get("component_amounts") or []
            if isinstance(value, Mapping)
        ),
        row_total=_decimal_or_none(item.get("row_total")),
        paid_marker_evidence=tuple(
            ObservablePaidMarker(
                page=_int_or_none(value.get("page")),
                text=_text(value.get("text")),
                bbox=_bbox(value.get("bbox")),
                confidence=_confidence(value.get("confidence")),
            )
            for value in item.get("paid_marker_evidence") or []
            if isinstance(value, Mapping)
        ),
        exclusion_reason=_text(item.get("exclusion_reason")),
    )


def _evidence(item: Mapping[str, Any]) -> IntermediateEvidenceReference:
    return IntermediateEvidenceReference(
        page=_int_or_none(item.get("page") or item.get("page_number")),
        text=_text(item.get("text")),
        normalized_text=_text(item.get("normalized_text")),
        bbox=_bbox(item.get("bbox")),
        source_type=_text(item.get("source_type")) or "document_observation",
        extraction_method=_text(item.get("extraction_method")) or "gemini_facts_transport",
        confidence=_confidence(item.get("confidence")),
    )


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = Decimal(str(value).replace("$", "").replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _decimal(value: Any) -> Decimal:
    return _decimal_or_none(value) or Decimal("0")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _confidence(value: Any) -> float | None:
    try:
        result = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return result if result is None else max(0.0, min(1.0, result))


def _bbox(value: Any) -> tuple[float, ...] | None:
    if isinstance(value, Mapping):
        value = [value.get(key) for key in ("x", "y", "w", "h")]
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        return tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "EligibleSupplementaryTarget",
    "InitialNormalizationCategory",
    "InitialNormalizationOutcome",
    "UnreconciledInvoiceObservation",
    "build_unreconciled_observation",
]
