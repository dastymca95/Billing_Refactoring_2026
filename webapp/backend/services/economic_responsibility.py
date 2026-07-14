"""Phase 3.8 economic responsibility contracts and deterministic classification."""
from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field, model_validator


class PaymentSourceType(str, Enum):
    MANAGEMENT_COMPANY_CARD = "management_company_card"
    MANAGEMENT_COMPANY_BANK = "management_company_bank"
    PROPERTY_CARD = "property_card"
    PROPERTY_BANK = "property_bank"
    PERSONAL_CARD = "personal_card"
    VENDOR_CREDIT = "vendor_credit"
    UNPAID_VENDOR_INVOICE = "unpaid_vendor_invoice"
    UNKNOWN = "unknown"


class EconomicBearerType(str, Enum):
    MANAGEMENT_COMPANY = "management_company"
    PROPERTY = "property"
    MULTIPLE_PROPERTIES = "multiple_properties"
    OWNER = "owner"
    EMPLOYEE = "employee"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class SettlementTreatment(str, Enum):
    CORPORATE_EXPENSE = "corporate_expense"
    PROPERTY_DIRECT_EXPENSE = "property_direct_expense"
    REIMBURSABLE_TO_MANAGEMENT_COMPANY = "reimbursable_to_management_company"
    INTERCOMPANY_DUE_TO_DUE_FROM = "intercompany_due_to_due_from"
    MULTI_PROPERTY_ALLOCATION = "multi_property_allocation"
    MIXED_LINE_LEVEL = "mixed_line_level"
    NON_AP_DOCUMENT = "non_ap_document"
    MANUAL_REVIEW = "manual_review"
    UNKNOWN = "unknown"


class AllocationScope(str, Enum):
    CORPORATE = "corporate"
    SINGLE_PROPERTY = "single_property"
    MULTIPLE_PROPERTIES = "multiple_properties"
    MIXED_LINE_LEVEL = "mixed_line_level"
    OWNER_LEVEL = "owner_level"
    UNKNOWN = "unknown"


class EvidenceStrength(str, Enum):
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"
    AUTHORITATIVE = "authoritative"


class ResponsibilityEvidence(BaseModel):
    evidence_type: str
    page: int | None = None
    region: list[float] | None = None
    text_summary: str | None = None
    source: str
    strength: EvidenceStrength
    supports: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class AllocationTarget(BaseModel):
    target_type: str
    target_reference: str
    percentage: Decimal | None = None
    amount: Decimal | None = None
    evidence: list[ResponsibilityEvidence] = Field(default_factory=list)


class LineResponsibility(BaseModel):
    line_item_id: str
    economic_bearer: EconomicBearerType
    settlement_treatment: SettlementTreatment
    allocation_scope: AllocationScope
    allocation_targets: list[AllocationTarget] = Field(default_factory=list)
    evidence: list[ResponsibilityEvidence] = Field(default_factory=list)
    review_required: bool = False
    review_reasons: list[str] = Field(default_factory=list)


class EconomicResponsibility(BaseModel):
    schema_version: str = "economic-responsibility/1.0"
    document_id: str
    payment_source: PaymentSourceType
    economic_bearer: EconomicBearerType
    settlement_treatment: SettlementTreatment
    allocation_scope: AllocationScope
    allocation_targets: list[AllocationTarget] = Field(default_factory=list)
    line_items: list[LineResponsibility] = Field(default_factory=list)
    evidence: list[ResponsibilityEvidence] = Field(default_factory=list)
    review_required: bool
    review_reasons: list[str] = Field(default_factory=list)
    deterministic: bool = True

    @model_validator(mode="after")
    def validate_allocations(self):
        _validate_targets(self.allocation_targets)
        for line in self.line_items:
            _validate_targets(line.allocation_targets)
        if self.settlement_treatment is SettlementTreatment.MIXED_LINE_LEVEL and len(self.line_items) < 2:
            raise ValueError("mixed_line_level requires at least two line decisions")
        return self


class MetadataCandidate(BaseModel):
    candidate_type: str
    normalized_value: str
    source_kind: str
    source_part_index: int
    confidence: float = Field(ge=0, le=1)
    authoritative: bool = False


class FilenameFolderFacts(BaseModel):
    schema_version: str = "filename-folder-facts/1.0"
    document_id: str
    original_filename: str
    parent_folder_parts: list[str] = Field(default_factory=list)
    normalized_filename: str
    normalized_folder_parts: list[str] = Field(default_factory=list)
    candidates: list[MetadataCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def to_evidence(self) -> list[ResponsibilityEvidence]:
        return [ResponsibilityEvidence(
            evidence_type=f"filename_context:{candidate.candidate_type}", source="filename_or_folder_context",
            strength=EvidenceStrength.WEAK, confidence=candidate.confidence,
            text_summary=f"Human-provided metadata candidate: {candidate.candidate_type}",
            supports=[f"metadata_candidate:{candidate.candidate_type}:{candidate.normalized_value}"],
        ) for candidate in self.candidates]


class FilenameFolderContextParser:
    """Extract generic metadata candidates without resolving accounting truth."""
    AMOUNT = re.compile(r"(?<!\w)(?:usd\s*)?\$?\s*(\d{1,7}(?:[,.]\d{2}))(?!\w)", re.I)
    DATE = re.compile(r"(?<!\d)(20\d{2})[-_ .](0?[1-9]|1[0-2])(?:[-_ .](0?[1-9]|[12]\d|3[01]))?(?!\d)")
    UNIT = re.compile(r"\b(?:unit|apt|suite|project)\s*[-#:]?\s*([a-z0-9-]{1,20})\b", re.I)
    CATEGORY_TERMS = frozenset({"maintenance", "education", "paint", "supplies", "meals", "subscription", "reimbursement"})

    def parse(self, document_id: str, original_filename: str, parent_folders: Iterable[str]) -> FilenameFolderFacts:
        folders = list(parent_folders)
        normalized_filename = _normalize_metadata_text(Path(original_filename).stem)
        normalized_folders = [_normalize_metadata_text(value) for value in folders]
        sources = [("filename", 0, normalized_filename), *[("folder", index, value) for index, value in enumerate(normalized_folders)]]
        candidates: list[MetadataCandidate] = []
        for source_kind, index, value in sources:
            for match in self.AMOUNT.finditer(value):
                amount = match.group(1).replace(",", "")
                candidates.append(MetadataCandidate(candidate_type="amount", normalized_value=amount,
                    source_kind=source_kind, source_part_index=index, confidence=.55))
            for match in self.DATE.finditer(value):
                date_value = "-".join(part.zfill(2) if offset else part for offset, part in enumerate(match.groups()) if part)
                candidates.append(MetadataCandidate(candidate_type="date", normalized_value=date_value,
                    source_kind=source_kind, source_part_index=index, confidence=.5))
            for match in self.UNIT.finditer(value):
                candidates.append(MetadataCandidate(candidate_type="unit_or_project", normalized_value=match.group(1).lower(),
                    source_kind=source_kind, source_part_index=index, confidence=.45))
            for token in value.split():
                if token in self.CATEGORY_TERMS:
                    candidates.append(MetadataCandidate(candidate_type="expense_category", normalized_value=token,
                        source_kind=source_kind, source_part_index=index, confidence=.4))
        warnings = ["filename_and_folder_context_is_non_authoritative"]
        if len({candidate.normalized_value for candidate in candidates if candidate.candidate_type == "amount"}) > 1:
            warnings.append("multiple_amount_candidates")
        return FilenameFolderFacts(document_id=document_id, original_filename=original_filename,
            parent_folder_parts=folders, normalized_filename=normalized_filename,
            normalized_folder_parts=normalized_folders, candidates=candidates, warnings=warnings)


class EconomicResponsibilityClassifier:
    """Conservative deterministic classifier over typed evidence, never GL."""
    def classify(self, document_id: str, evidence: Iterable[ResponsibilityEvidence],
                 line_items: Iterable[LineResponsibility] = ()) -> EconomicResponsibility:
        evidence = list(evidence); lines = list(line_items)
        claims, contradictions = _score_claims(evidence)
        payment = _choose_enum(PaymentSourceType, "payment_source", claims, contradictions)
        bearer = _choose_enum(EconomicBearerType, "economic_bearer", claims, contradictions)
        allocation = _choose_enum(AllocationScope, "allocation_scope", claims, contradictions)
        explicit_settlement = _choose_enum(SettlementTreatment, "settlement_treatment", claims, contradictions)
        reasons: list[str] = []
        distinct_line_treatments = {line.settlement_treatment for line in lines}
        if "document_role:non_ap" in claims and claims["document_role:non_ap"] >= 1.5:
            settlement = SettlementTreatment.NON_AP_DOCUMENT
        elif len(distinct_line_treatments) > 1:
            settlement = SettlementTreatment.MIXED_LINE_LEVEL
            bearer = EconomicBearerType.MIXED
            allocation = AllocationScope.MIXED_LINE_LEVEL
        elif explicit_settlement is not SettlementTreatment.UNKNOWN:
            settlement = explicit_settlement
        elif payment in {PaymentSourceType.MANAGEMENT_COMPANY_CARD, PaymentSourceType.MANAGEMENT_COMPANY_BANK} \
                and bearer is EconomicBearerType.PROPERTY and allocation is AllocationScope.SINGLE_PROPERTY:
            settlement = SettlementTreatment.REIMBURSABLE_TO_MANAGEMENT_COMPANY
        elif payment in {PaymentSourceType.PROPERTY_CARD, PaymentSourceType.PROPERTY_BANK} \
                and bearer is EconomicBearerType.PROPERTY:
            settlement = SettlementTreatment.PROPERTY_DIRECT_EXPENSE
        elif bearer is EconomicBearerType.MANAGEMENT_COMPANY and allocation is AllocationScope.CORPORATE:
            settlement = SettlementTreatment.CORPORATE_EXPENSE
        elif bearer is EconomicBearerType.MULTIPLE_PROPERTIES or allocation is AllocationScope.MULTIPLE_PROPERTIES:
            settlement = SettlementTreatment.MULTI_PROPERTY_ALLOCATION
        else:
            settlement = SettlementTreatment.MANUAL_REVIEW
            reasons.append("insufficient_evidence_for_settlement")
        if payment is PaymentSourceType.UNKNOWN: reasons.append("payment_source_unknown")
        if bearer is EconomicBearerType.UNKNOWN: reasons.append("economic_bearer_unknown")
        if allocation is AllocationScope.UNKNOWN: reasons.append("allocation_scope_unknown")
        if any(value > 0 for value in contradictions.values()): reasons.append("contradictory_responsibility_evidence")
        review = settlement in {SettlementTreatment.MANUAL_REVIEW, SettlementTreatment.UNKNOWN} or bool(reasons)
        return EconomicResponsibility(document_id=document_id, payment_source=payment,
            economic_bearer=bearer, settlement_treatment=settlement, allocation_scope=allocation,
            line_items=lines, evidence=evidence, review_required=review, review_reasons=sorted(set(reasons)))


def _score_claims(evidence: list[ResponsibilityEvidence]):
    weights = {EvidenceStrength.WEAK: .25, EvidenceStrength.MODERATE: .75,
               EvidenceStrength.STRONG: 1.5, EvidenceStrength.AUTHORITATIVE: 3.0}
    supports: dict[str, float] = defaultdict(float); contradicts: dict[str, float] = defaultdict(float)
    for item in evidence:
        weight = weights[item.strength] * item.confidence
        for claim in item.supports: supports[claim] += weight
        for claim in item.contradicts: contradicts[claim] += weight
    return supports, contradicts


def _choose_enum(enum_type, prefix: str, claims, contradictions):
    ranked = []
    for member in enum_type:
        if member.value == "unknown": continue
        claim = f"{prefix}:{member.value}"
        score = claims.get(claim, 0) - contradictions.get(claim, 0)
        if score > 0: ranked.append((score, member))
    ranked.sort(key=lambda item: (-item[0], item[1].value))
    if not ranked or ranked[0][0] < .7: return enum_type.UNKNOWN
    if len(ranked) > 1 and abs(ranked[0][0] - ranked[1][0]) < .25: return enum_type.UNKNOWN
    return ranked[0][1]


def _validate_targets(targets: list[AllocationTarget]):
    percentages = [target.percentage for target in targets if target.percentage is not None]
    if percentages and abs(sum(percentages, Decimal("0")) - Decimal("100")) > Decimal("0.01"):
        raise ValueError("allocation percentages must sum to 100")
    if len({target.target_reference for target in targets}) != len(targets):
        raise ValueError("allocation target references must be unique")


def _normalize_metadata_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9$.,#-]+", " ", value.lower())).strip()
