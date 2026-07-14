"""Versioned contracts separating document evidence from accounting decisions."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

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
