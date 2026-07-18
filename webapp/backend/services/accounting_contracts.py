"""Versioned contracts separating document evidence from accounting decisions."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class EvidenceReference(BaseModel):
    document_id: str
    page: int | None = None
    text: str | None = None
    normalized_text: str | None = None
    bbox: list[float] | None = None
    source_type: str
    extraction_method: str
    confidence: float | None = None


class CropCoordinates(BaseModel):
    """Pixel coordinates on one immutable rendered source page."""

    page: int
    x: int
    y: int
    width: int
    height: int
    render_dpi: int
    source_page_width: int | None = None
    source_page_height: int | None = None


class RowIdentityAlternative(BaseModel):
    value: str
    confidence: float


class HandwrittenRowIdentityEvidence(BaseModel):
    """Observed handwriting, kept separate from catalog validation."""

    raw_value: str | None = None
    alternatives: list[RowIdentityAlternative] = Field(default_factory=list)
    confidence: float
    crop_coordinates: CropCoordinates
    catalog_matches: list[str] = Field(default_factory=list)
    resolved_unit: str | None = None
    status: Literal["confirmed", "needs_confirmation", "illegible"]
    resolution_basis: str


class PaidMarkerEvidence(BaseModel):
    page: int
    text: str
    bbox: list[float] | None = None
    confidence: float | None = None


class ExcludedPaidRowFacts(BaseModel):
    """Immutable source facts for a visible row excluded because it is PAID."""

    raw_apartment_number: str | None = None
    apartment_identity: HandwrittenRowIdentityEvidence | None = None
    component_amounts: dict[str, Decimal] = Field(default_factory=dict)
    row_total: Decimal | None = None
    paid_marker_evidence: list[PaidMarkerEvidence] = Field(default_factory=list)
    exclusion_reason: str


class DateFieldProvenance(BaseModel):
    field: Literal["service_date", "invoice_date", "due_date_text", "due_date"]
    value: str | None = None
    raw_value: str | None = None
    provenance: Literal["document_observed", "tenant_policy_inference", "unresolved"]
    source_field: str | None = None
    policy_id: str | None = None
    evidence: list[EvidenceReference] = Field(default_factory=list)


class LineItemFacts(BaseModel):
    line_item_id: str
    raw_activity: str | None = None
    raw_description: str | None = None
    normalized_activity: str | None = None
    normalized_description: str | None = None
    generated_description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    tax: Decimal | None = None
    detected_location: str | None = None
    evidence: list[EvidenceReference] = Field(default_factory=list)


class DocumentFacts(BaseModel):
    schema_version: str = "document-facts/1.0"
    document_id: str
    invoice_id: str
    vendor_candidate: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    service_address: str | None = None
    property_candidate: str | None = None
    total_amount: Decimal | None = None
    document_family_candidate: str | None = None
    line_items: list[LineItemFacts]
    extraction_route: str
    extraction_model: str | None = None
    evidence: list[EvidenceReference] = Field(default_factory=list)


class SemanticClassification(BaseModel):
    semantic_version: str
    line_item_id: str
    document_family: str
    line_family: str
    trade_family: str
    work_mode: str
    recurrence: str
    capital_context: str
    specific_assets: list[str] = Field(default_factory=list)
    location_detected: str | None = None
    positive_evidence: list[EvidenceReference] = Field(default_factory=list)
    negative_evidence: list[EvidenceReference] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    confidence: float


class GLAccountMetadata(BaseModel):
    gl_code: str
    gl_name: str
    gl_family: str
    trade_families: list[str] = Field(default_factory=list)
    compatible_work_modes: list[str] = Field(default_factory=list)
    incompatible_work_modes: list[str] = Field(default_factory=list)
    capital_context: str = "operating"
    specificity: str = "broad"
    payable: bool
    description_tokens: list[str] = Field(default_factory=list)
    scope_qualifiers: list[str] = Field(default_factory=list)
    metadata_source: str
    metadata_confidence: float


class GLCandidate(BaseModel):
    gl_code: str
    gl_name: str
    source: str
    source_id: str | None = None
    base_score: float
    positive_evidence: list[dict[str, Any]] = Field(default_factory=list)
    negative_evidence: list[dict[str, Any]] = Field(default_factory=list)
    compatibility_results: list[dict[str, Any]] = Field(default_factory=list)
    score_components: dict[str, float] = Field(default_factory=dict)
    rule_version: str | None = None


class AccountingDecision(BaseModel):
    decision_id: str
    decision_version: str
    line_item_id: str
    selected_gl_code: str | None = None
    selected_gl_name: str | None = None
    decision_source: str
    confidence: float
    why_selected: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    candidates_ranked: list[GLCandidate] = Field(default_factory=list)
    rejected_alternatives: list[dict[str, Any]] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    review_required: bool
    review_blocking: bool
    review_reason: str | None = None
    catalog_version: str
    semantic_version: str


def model_dict(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else model.dict()
