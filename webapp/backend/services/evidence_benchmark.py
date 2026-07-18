"""Evidence-backed benchmark contracts for private accounting documents.

The verifier is deliberately not an adjudicator.  A record becomes gold only
after a human reviewer confirms the pixels referenced by the immutable source
hash and crop hash.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .accounting_contracts import CropCoordinates


GOLDEN_CONTRACT_VERSION = "evidence-backed-accounting-golden/1.0"


class AdjudicationState(str, Enum):
    PENDING_HUMAN_REVIEW = "pending_human_review"
    CONFLICT = "conflict"
    ADJUDICATED = "adjudicated"


class ExportSafetyExpectation(str, Enum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"


class EvidenceAsset(BaseModel):
    source_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_page: int = Field(ge=1)
    crop_coordinates: CropCoordinates
    crop_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    crop_ref: str

    @model_validator(mode="after")
    def safe_relative_reference(self):
        ref = Path(self.crop_ref)
        if ref.is_absolute() or ".." in ref.parts:
            raise ValueError("crop_ref must be a safe relative reference")
        if self.crop_coordinates.page != self.source_page:
            raise ValueError("crop page must equal source_page")
        return self


class ObservedCandidate(BaseModel):
    value: str | None = None
    normalized_value: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    source: Literal["cold_extractor", "prior_accepted_run", "targeted_verifier", "human_reviewer"]


class VerifierObservation(BaseModel):
    verifier_id: str
    observed_at: datetime
    raw_value: str | None = None
    alternatives: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence: EvidenceAsset
    request_id: str | None = None


class HumanAdjudication(BaseModel):
    reviewer_id: str
    adjudicated_at: datetime
    accepted_value: str | None = None
    acceptable_alternatives: list[str] = Field(default_factory=list)
    rationale: str


class EvidenceBackedField(BaseModel):
    field_name: str
    observed_raw_text: str | None = None
    accepted_normalized_value: str | None = None
    acceptable_alternatives: list[str] = Field(default_factory=list)
    candidates: list[ObservedCandidate] = Field(default_factory=list)
    evidence: list[EvidenceAsset] = Field(default_factory=list)
    verifier_observations: list[VerifierObservation] = Field(default_factory=list)
    state: AdjudicationState = AdjudicationState.PENDING_HUMAN_REVIEW
    human_adjudication: HumanAdjudication | None = None

    @model_validator(mode="after")
    def enforce_human_gold_boundary(self):
        if self.state is AdjudicationState.ADJUDICATED:
            if self.human_adjudication is None:
                raise ValueError("adjudicated fields require a human adjudication")
            if not self.evidence:
                raise ValueError("adjudicated fields require source evidence")
            if self.accepted_normalized_value != self.human_adjudication.accepted_value:
                raise ValueError("accepted value must be the human-adjudicated value")
        elif self.human_adjudication is not None:
            raise ValueError("human adjudication requires adjudicated state")
        return self


class GoldenRowContract(BaseModel):
    row_id: str
    source_page: int = Field(ge=1)
    row_identity: EvidenceBackedField
    paid_crossed_out_status: EvidenceBackedField
    line_item_concept: EvidenceBackedField
    amount: EvidenceBackedField
    canonical_semantic_concept: str | None = None
    acceptable_gl_set: list[str] = Field(default_factory=list)
    expected_gl: str | None = None
    required_review_categories: list[str] = Field(default_factory=list)
    export_safety_expectation: ExportSafetyExpectation = ExportSafetyExpectation.BLOCKED

    @model_validator(mode="after")
    def validate_gl_expectation(self):
        if self.expected_gl and self.acceptable_gl_set and self.expected_gl not in self.acceptable_gl_set:
            raise ValueError("expected_gl must belong to acceptable_gl_set")
        return self


class GoldenInvoiceContract(BaseModel):
    invoice_id: str
    source_file_name: str
    source_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_page: int = Field(ge=1)
    header_fields: dict[str, EvidenceBackedField] = Field(default_factory=dict)
    rows: list[GoldenRowContract]
    excluded_rows: list[GoldenRowContract] = Field(default_factory=list)
    required_review_categories: list[str] = Field(default_factory=list)
    export_safety_expectation: ExportSafetyExpectation = ExportSafetyExpectation.BLOCKED


class EvidenceBackedGoldenContract(BaseModel):
    schema_version: str = GOLDEN_CONTRACT_VERSION
    batch_id: str
    created_at: datetime
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: AdjudicationState = AdjudicationState.PENDING_HUMAN_REVIEW
    invoices: list[GoldenInvoiceContract]

    @model_validator(mode="after")
    def validate_contract(self):
        ids = [invoice.invoice_id for invoice in self.invoices]
        if len(ids) != len(set(ids)):
            raise ValueError("invoice_id values must be unique")
        if self.state is AdjudicationState.ADJUDICATED:
            fields = [field for invoice in self.invoices for field in invoice.header_fields.values()]
            fields += [field for invoice in self.invoices for row in invoice.rows + invoice.excluded_rows
                       for field in (row.row_identity, row.paid_crossed_out_status,
                                     row.line_item_concept, row.amount)]
            if any(field.state is not AdjudicationState.ADJUDICATED for field in fields):
                raise ValueError("an adjudicated contract cannot contain non-adjudicated fields")
        return self


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "AdjudicationState", "EvidenceAsset", "EvidenceBackedField",
    "EvidenceBackedGoldenContract", "ExportSafetyExpectation",
    "GoldenInvoiceContract", "GoldenRowContract", "HumanAdjudication",
    "ObservedCandidate", "VerifierObservation", "canonical_sha256", "file_sha256",
]
