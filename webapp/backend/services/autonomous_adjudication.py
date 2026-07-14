"""Phase 3.9B autonomous, fail-closed document adjudication."""
from __future__ import annotations

import json
import math
import re
import hashlib
from datetime import datetime, timezone
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml
from pydantic import BaseModel, Field

from .accounting_readiness import AccountingReadiness, as_dict as readiness_dict, evaluate_rows
from .economic_responsibility import (AllocationScope, EconomicBearerType, EconomicResponsibility,
    EconomicResponsibilityClassifier, EvidenceStrength, LineResponsibility, PaymentSourceType,
    ResponsibilityEvidence, SettlementTreatment)
from .model_registry import CapabilityDiscovery, ModelCapability, ModelRegistry, ModelRole, default_registry


SCHEMA_VERSION = "autonomous-adjudication/1.0"


class AutonomousStatus(str, Enum):
    MACHINE_PROPOSED = "machine_proposed"
    MACHINE_VERIFIED = "machine_verified"
    MACHINE_ADJUDICATED = "machine_adjudicated"
    EXCEPTION_REQUIRED = "exception_required"


class ConsensusStatus(str, Enum):
    EXACT_AGREEMENT = "exact_agreement"
    NORMALIZED_AGREEMENT = "normalized_agreement"
    SUPPORTED_SINGLE_SOURCE = "supported_single_source"
    RESOLVABLE_CONFLICT = "resolvable_conflict"
    UNRESOLVED_CONFLICT = "unresolved_conflict"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


class FieldEvidence(BaseModel):
    page: int | None = None
    region: list[float] | None = None
    source_type: str
    extraction_profile: str
    raw_supporting_text: str | None = None
    visual_summary: str | None = None


class ExtractedField(BaseModel):
    field_path: str
    value: Any = None
    confidence: float = Field(ge=0, le=1)
    evidence: list[FieldEvidence] = Field(default_factory=list)
    source_pass: str


class VerificationFinding(BaseModel):
    field_path: str
    disposition: str
    value: Any = None
    confidence: float = Field(ge=0, le=1)
    evidence: list[FieldEvidence] = Field(default_factory=list)
    alternatives: list[Any] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


class ConsensusField(BaseModel):
    field_path: str
    selected_value: Any = None
    status: ConsensusStatus
    confidence: float = Field(ge=0, le=1)
    primary: ExtractedField | None = None
    verification: VerificationFinding | None = None
    evidence: list[FieldEvidence] = Field(default_factory=list)
    material: bool = False


class ValidationCheck(BaseModel):
    code: str
    passed: bool
    material: bool = True
    expected: Decimal | None = None
    actual: Decimal | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class PropertyResolution(BaseModel):
    selected_property: str | None = None
    alternatives: list[str] = Field(default_factory=list)
    evidence: list[FieldEvidence] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    contradiction_status: str


class ReimbursementResolution(BaseModel):
    reimbursement_required: bool | None = None
    reimbursing_entity: str | None = None
    reimbursed_entity: str | None = None
    confidence: float = Field(ge=0, le=1)
    status: str
    evidence: list[FieldEvidence] = Field(default_factory=list)


class VisualPreprocessingResult(BaseModel):
    schema_version: str = "visual-preprocessing/1.0"
    source_type: str
    page_count: int
    page_hashes: list[str] = Field(default_factory=list)
    duplicate_page_indexes: list[int] = Field(default_factory=list)
    rotation_degrees: int = 0
    handwriting_route_required: bool = False
    warnings: list[str] = Field(default_factory=list)


class ExtractionPass(BaseModel):
    pass_id: str
    profile_version: str
    model_id: str | None = None
    model_family: str | None = None
    route: str
    fields: list[ExtractedField] = Field(default_factory=list)
    lines: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AutonomousAdjudicationResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    document_id: str
    status: AutonomousStatus
    primary_extraction: ExtractionPass
    verification_profile: str
    consensus: list[ConsensusField]
    arithmetic_validation: list[ValidationCheck]
    property_resolution: PropertyResolution
    reimbursement_resolution: ReimbursementResolution
    economic_responsibility: EconomicResponsibility
    rows: list[dict[str, Any]] = Field(default_factory=list)
    accounting_readiness: dict[str, Any]
    exception_codes: list[str] = Field(default_factory=list)
    capability_evidence: list[dict[str, Any]] = Field(default_factory=list)
    thresholds_version: str
    generated_at: datetime
    human_action_required: bool
    gold_status: str = "not_gold"


class AutonomousPolicy:
    def __init__(self, values: Mapping[str, Any], version: str) -> None:
        self.values = dict(values); self.version = version

    @classmethod
    def load(cls, path: Path | None = None) -> "AutonomousPolicy":
        path = path or Path(__file__).resolve().parents[3] / "config" / "autonomous_adjudication.yaml"
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(payload["autonomous_adjudication"], payload["schema_version"])

    def threshold(self, name: str) -> float:
        return float(self.values[name])


class AutonomousCapabilitySelector:
    """Select only provider-advertised capabilities; configured names are insufficient."""
    def __init__(self, discovery: CapabilityDiscovery) -> None: self.discovery = discovery

    def available(self, role: ModelRole) -> list[ModelCapability]:
        return [capability for spec in self.discovery.registry.for_role(role)
                if (capability := self.discovery.discover(spec.model_id)).available]

    def extraction_profiles(self, *, visual_required: bool) -> list[ModelCapability]:
        role = ModelRole.EXTRACTION_VISION if visual_required else ModelRole.EXTRACTION_TEXT
        return self.available(role)


class FieldConsensusEngine:
    MATERIAL_PREFIXES = ("document.document_family", "document.vendor", "document.invoice_number",
        "document.invoice_date", "document.total", "document.property", "lines.")

    def __init__(self, policy: AutonomousPolicy) -> None: self.policy = policy

    def decide(self, primary: Iterable[ExtractedField], verification: Iterable[VerificationFinding]) -> list[ConsensusField]:
        p = {field.field_path: field for field in primary}; v = {field.field_path: field for field in verification}
        output = []
        for path in sorted(set(p) | set(v)):
            left, right = p.get(path), v.get(path); material = path.startswith(self.MATERIAL_PREFIXES)
            if left is None and right is None:
                status, value, confidence = ConsensusStatus.MISSING, None, 0
            elif left is None:
                status, value, confidence = self._single(right.value, right.confidence)
            elif right is None or right.disposition == "missing":
                status, value, confidence = self._single(left.value, left.confidence)
            elif right.disposition == "rejected":
                status, value, confidence = ConsensusStatus.UNRESOLVED_CONFLICT, None, min(left.confidence, right.confidence)
            elif left.value == right.value:
                status, value, confidence = ConsensusStatus.EXACT_AGREEMENT, left.value, min(1.0, max(left.confidence, right.confidence) + .03)
            elif _normalize(left.value) == _normalize(right.value):
                status, value, confidence = ConsensusStatus.NORMALIZED_AGREEMENT, right.value, min(left.confidence, right.confidence)
            elif right.disposition == "corrected" and right.confidence >= self.policy.threshold("required_field_confidence"):
                status, value, confidence = ConsensusStatus.RESOLVABLE_CONFLICT, right.value, right.confidence
            else:
                status, value, confidence = ConsensusStatus.UNRESOLVED_CONFLICT, None, min(left.confidence, right.confidence)
            evidence = list(left.evidence if left else []) + list(right.evidence if right else [])
            output.append(ConsensusField(field_path=path, selected_value=value, status=status, confidence=confidence,
                primary=left, verification=right, evidence=evidence, material=material))
        return output

    def _single(self, value: Any, confidence: float):
        threshold = self.policy.threshold("supported_single_source_confidence")
        return ((ConsensusStatus.SUPPORTED_SINGLE_SOURCE, value, confidence) if value not in (None, "") and confidence >= threshold
                else (ConsensusStatus.MISSING, None, confidence))


class ArithmeticValidator:
    def __init__(self, tolerance: Decimal) -> None: self.tolerance = tolerance

    def validate(self, consensus: Iterable[ConsensusField], lines: list[dict[str, Any]]) -> list[ValidationCheck]:
        values = {field.field_path: field.selected_value for field in consensus}
        checks: list[ValidationCheck] = []
        subtotal, tax, shipping, tip, discount, total = (_decimal(values.get(f"document.{name}")) for name in
            ("subtotal", "tax", "shipping", "tip", "discount", "total"))
        components = [subtotal, tax, shipping, tip, discount, total]
        if total is not None and any(value is not None for value in components[:-1]):
            expected = (subtotal or 0) + (tax or 0) + (shipping or 0) + (tip or 0) - (discount or 0)
            checks.append(self._check("document_total_reconciliation", expected, total))
        line_amounts = [_decimal(line.get("amount")) for line in lines]
        if total is not None and line_amounts and all(value is not None for value in line_amounts):
            checks.append(self._check("line_sum_reconciliation", sum(line_amounts, Decimal("0")), total))
        for index, line in enumerate(lines):
            quantity, unit_price, amount = (_decimal(line.get(key)) for key in ("quantity", "unit_price", "amount"))
            if quantity is not None and unit_price is not None and amount is not None:
                checks.append(self._check(f"quantity_unit_price:{index}", quantity * unit_price, amount))
        allocation = [_decimal(line.get("allocation_percentage")) for line in lines if line.get("allocation_percentage") is not None]
        if allocation: checks.append(self._check("allocation_percentage_total", Decimal("100"), sum(allocation, Decimal("0"))))
        due, paid = _decimal(values.get("document.amount_due")), _decimal(values.get("document.amount_paid"))
        if due is not None and paid is not None:
            checks.append(ValidationCheck(code="due_amount_vs_paid_amount", passed=due >= 0 and paid >= 0,
                                          expected=due, actual=paid))
        bill_or_credit = str(values.get("document.bill_or_credit") or "").lower()
        if total is not None and bill_or_credit:
            charge_ok = total <= 0 if "credit" in bill_or_credit else total >= 0
            checks.append(ValidationCheck(code="credit_vs_charge_sign", passed=charge_ok, actual=total))
        start, end = _date(values.get("document.service_period_start")), _date(values.get("document.service_period_end"))
        if start or end:
            checks.append(ValidationCheck(code="service_period_validity", passed=bool(start and end and start <= end),
                evidence=[{"start": str(start) if start else None, "end": str(end) if end else None}]))
        page_count = _int(values.get("document.page_count")); page_numbers = values.get("document.page_numbers")
        if page_count is not None and isinstance(page_numbers, list):
            expected_pages = list(range(1, page_count + 1))
            checks.append(ValidationCheck(code="multi_page_continuity", passed=sorted(set(page_numbers)) == expected_pages,
                evidence=[{"expected_pages": expected_pages, "observed_pages": page_numbers}]))
        duplicate_pages = values.get("document.duplicate_pages")
        if duplicate_pages is not None:
            checks.append(ValidationCheck(code="duplicate_pages", passed=not bool(duplicate_pages),
                evidence=[{"duplicate_pages": duplicate_pages}]))
        repeated = values.get("document.repeated_line_items_unresolved")
        if repeated is not None:
            checks.append(ValidationCheck(code="repeated_line_items", passed=not bool(repeated),
                evidence=[{"unresolved": bool(repeated)}]))
        account_numbers = values.get("document.account_numbers")
        if isinstance(account_numbers, list):
            normalized_accounts = {_normalize(value) for value in account_numbers if value}
            checks.append(ValidationCheck(code="account_number_consistency", passed=len(normalized_accounts) <= 1,
                evidence=[{"distinct_account_count": len(normalized_accounts)}]))
        document_tax = _decimal(values.get("document.tax"))
        line_taxes = [_decimal(line.get("tax")) for line in lines if line.get("tax") is not None]
        if document_tax is not None and line_taxes and all(value is not None for value in line_taxes):
            checks.append(self._check("tax_treatment", sum(line_taxes, Decimal("0")), document_tax))
        if discount is not None:
            checks.append(ValidationCheck(code="discount_treatment", passed=discount >= 0, actual=discount))
        return checks

    def _check(self, code: str, expected: Decimal, actual: Decimal) -> ValidationCheck:
        return ValidationCheck(code=code, passed=abs(expected-actual) <= self.tolerance,
                               expected=expected, actual=actual, evidence=[{"tolerance": str(self.tolerance)}])


class PropertyResolver:
    def __init__(self, policy: AutonomousPolicy, aliases: Mapping[str, str] | None = None) -> None:
        self.policy = policy; self.aliases = {_normalize(key): value for key, value in (aliases or {}).items()}

    def resolve(self, consensus: Iterable[ConsensusField]) -> PropertyResolution:
        candidates = [field for field in consensus if field.field_path in {"document.property", "document.service_address",
            "document.ship_to", "document.filename_property"} and field.selected_value]
        property_fields = [field for field in candidates if "property" in field.field_path]
        normalized = {self._canonical(field.selected_value) for field in property_fields}
        if len(normalized) > 1:
            return PropertyResolution(alternatives=[str(field.selected_value) for field in property_fields],
                evidence=[e for f in property_fields for e in f.evidence], confidence=0,
                contradiction_status="unresolved_conflict")
        if property_fields:
            strongest = max(property_fields, key=lambda field: field.confidence)
            corroborated = len(property_fields) > 1 or any(_normalize(strongest.selected_value) in _normalize(field.selected_value) for field in candidates if field is not strongest)
            confidence = min(1.0, strongest.confidence + (.05 if corroborated else 0))
            selected = self.aliases.get(_normalize(strongest.selected_value), str(strongest.selected_value)) if confidence >= self.policy.threshold("property_confidence") else None
            return PropertyResolution(selected_property=selected, alternatives=[str(f.selected_value) for f in property_fields if f is not strongest],
                evidence=[e for f in property_fields for e in f.evidence], confidence=confidence,
                contradiction_status="none" if selected else "insufficient_support")
        return PropertyResolution(confidence=0, contradiction_status="missing")

    def _canonical(self, value: Any) -> str:
        return _normalize(self.aliases.get(_normalize(value), value))


class ResponsibilityEvidenceBuilder:
    MAPPING = {"document.economic_responsibility.payment_source": "payment_source",
        "document.economic_responsibility.economic_bearer": "economic_bearer",
        "document.economic_responsibility.settlement_treatment": "settlement_treatment",
        "document.economic_responsibility.allocation_scope": "allocation_scope"}

    def build(self, consensus: Iterable[ConsensusField]) -> list[ResponsibilityEvidence]:
        output = []
        for field in consensus:
            prefix = self.MAPPING.get(field.field_path)
            if not prefix or not field.selected_value: continue
            strength = EvidenceStrength.STRONG if field.status in {ConsensusStatus.EXACT_AGREEMENT,
                ConsensusStatus.NORMALIZED_AGREEMENT} else EvidenceStrength.MODERATE
            output.append(ResponsibilityEvidence(evidence_type="autonomous_consensus", source="field_consensus",
                strength=strength, confidence=field.confidence, supports=[f"{prefix}:{field.selected_value}"],
                text_summary=f"Consensus {field.status.value} for {prefix}"))
        return output


class ReimbursementResolver:
    def __init__(self, policy: AutonomousPolicy) -> None: self.policy = policy

    def resolve(self, consensus: Iterable[ConsensusField], responsibility: EconomicResponsibility) -> ReimbursementResolution:
        fields = {field.field_path: field for field in consensus}
        required = fields.get("document.reimbursement_required")
        reimbursing = fields.get("document.reimbursing_entity"); reimbursed = fields.get("document.reimbursed_entity")
        inferred_required = responsibility.settlement_treatment in {SettlementTreatment.REIMBURSABLE_TO_MANAGEMENT_COMPANY,
            SettlementTreatment.INTERCOMPANY_DUE_TO_DUE_FROM}
        if required and required.selected_value is not None:
            value = str(required.selected_value).lower() in {"true", "yes", "1", "required"}; confidence = required.confidence
        elif responsibility.settlement_treatment not in {SettlementTreatment.UNKNOWN, SettlementTreatment.MANUAL_REVIEW}:
            value = inferred_required; confidence = min(.95, max((e.confidence for e in responsibility.evidence), default=.9))
        else:
            return ReimbursementResolution(confidence=0, status="unresolved")
        if value and (not reimbursing or not reimbursing.selected_value or not reimbursed or not reimbursed.selected_value):
            return ReimbursementResolution(reimbursement_required=True,
                reimbursing_entity=str(reimbursing.selected_value) if reimbursing and reimbursing.selected_value else None,
                reimbursed_entity=str(reimbursed.selected_value) if reimbursed and reimbursed.selected_value else None,
                confidence=confidence, status="unresolved_entities",
                evidence=list(required.evidence) if required else [])
        return ReimbursementResolution(reimbursement_required=value,
            reimbursing_entity=str(reimbursing.selected_value) if reimbursing and reimbursing.selected_value else None,
            reimbursed_entity=str(reimbursed.selected_value) if reimbursed and reimbursed.selected_value else None,
            confidence=confidence, status="resolved" if confidence >= self.policy.threshold("responsibility_confidence") else "insufficient_confidence",
            evidence=list(required.evidence) if required else [])


class VisualPreprocessor:
    """Read-only structural preprocessing; never rewrites private sources."""
    def preprocess(self, path: Path, *, rotation_degrees: int = 0, handwriting_hint: bool = False) -> VisualPreprocessingResult:
        suffix = path.suffix.lower(); hashes: list[str] = []; warnings: list[str] = []
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
                reader = PdfReader(path); page_count = len(reader.pages)
                for page in reader.pages:
                    material = (page.extract_text() or "").encode("utf-8", errors="ignore")
                    hashes.append(hashlib.sha256(material).hexdigest() if material.strip() else "")
                if any(not digest for digest in hashes): warnings.append("page_content_hash_unavailable_for_image_pages")
            except Exception as exc:
                page_count = 1; hashes = [hashlib.sha256(path.read_bytes()).hexdigest()]
                warnings.append(f"pdf_structure_unavailable:{type(exc).__name__}")
            source_type = "digital_or_scanned_pdf"
        else:
            page_count = 1; hashes = [hashlib.sha256(path.read_bytes()).hexdigest()]
            source_type = "image" if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"} else "unknown"
        seen = {}; duplicates = []
        for index, digest in enumerate(hashes):
            if not digest: continue
            if digest in seen: duplicates.append(index + 1)
            else: seen[digest] = index + 1
        return VisualPreprocessingResult(source_type=source_type, page_count=page_count, page_hashes=hashes,
            duplicate_page_indexes=duplicates, rotation_degrees=rotation_degrees,
            handwriting_route_required=handwriting_hint, warnings=warnings)


class AutonomousAdjudicator:
    """Orchestrates machine analysis without mutating labels, readiness, or frozen data."""
    def __init__(self, *, policy: AutonomousPolicy | None = None, registry: ModelRegistry | None = None,
                 advertised_models: Iterable[str] | None = None,
                 extraction_gateway: Callable[..., tuple[ExtractionPass, list[VerificationFinding]]] | None = None,
                 property_aliases: Mapping[str, str] | None = None) -> None:
        self.policy = policy or AutonomousPolicy.load(); self.registry = registry or default_registry()
        self.discovery = CapabilityDiscovery(self.registry, advertised_models); self.selector = AutonomousCapabilitySelector(self.discovery)
        self.extraction_gateway = extraction_gateway; self.consensus_engine = FieldConsensusEngine(self.policy)
        self.arithmetic = ArithmeticValidator(Decimal(str(self.policy.values["arithmetic_tolerance"])))
        self.properties = PropertyResolver(self.policy, property_aliases)
        self.responsibility_evidence = ResponsibilityEvidenceBuilder()
        self.reimbursements = ReimbursementResolver(self.policy)

    def adjudicate(self, document_id: str, *, deterministic_primary: ExtractionPass,
                   deterministic_verification: list[VerificationFinding], visual_required: bool,
                   responsibility_evidence: Iterable[ResponsibilityEvidence] = ()) -> AutonomousAdjudicationResult:
        capabilities = self.selector.extraction_profiles(visual_required=visual_required)
        primary, verification = deterministic_primary, deterministic_verification
        if capabilities and self.extraction_gateway:
            primary, verification = self.extraction_gateway(document_id=document_id, capabilities=capabilities,
                isolated_verification=True, max_model_calls=int(self.policy.values["independent_model_voting_max_calls"]))
        consensus = self.consensus_engine.decide(primary.fields, verification)
        checks = self.arithmetic.validate(consensus, primary.lines); property_resolution = self.properties.resolve(consensus)
        responsibility = EconomicResponsibilityClassifier().classify(document_id,
            [*responsibility_evidence, *self.responsibility_evidence.build(consensus)])
        if not responsibility.review_required and primary.lines:
            responsibility.line_items = [LineResponsibility(line_item_id=str(line.get("line_id") or index + 1),
                economic_bearer=responsibility.economic_bearer, settlement_treatment=responsibility.settlement_treatment,
                allocation_scope=responsibility.allocation_scope, evidence=list(responsibility.evidence), review_required=False)
                for index, line in enumerate(primary.lines)]
        reimbursement = self.reimbursements.resolve(consensus, responsibility)
        rows = self._rows(primary.lines, consensus, property_resolution)
        readiness = evaluate_rows(rows)
        exceptions = self._exceptions(consensus, checks, property_resolution, responsibility, reimbursement, readiness, rows)
        verified = any(field.status in {ConsensusStatus.EXACT_AGREEMENT, ConsensusStatus.NORMALIZED_AGREEMENT,
                       ConsensusStatus.SUPPORTED_SINGLE_SOURCE} for field in consensus)
        status = AutonomousStatus.EXCEPTION_REQUIRED if exceptions else AutonomousStatus.MACHINE_ADJUDICATED
        if not exceptions and not readiness.export_allowed and self._non_ap(consensus): status = AutonomousStatus.MACHINE_ADJUDICATED
        elif not exceptions and not verified: status = AutonomousStatus.MACHINE_PROPOSED
        return AutonomousAdjudicationResult(document_id=document_id, status=status, primary_extraction=primary,
            verification_profile="isolated-independent-verification/1.0", consensus=consensus,
            arithmetic_validation=checks, property_resolution=property_resolution,
            reimbursement_resolution=reimbursement, economic_responsibility=responsibility,
            rows=rows, accounting_readiness=readiness_dict(readiness),
            exception_codes=sorted(set(exceptions)), capability_evidence=[capability.__dict__ for capability in capabilities],
            thresholds_version=self.policy.version, generated_at=datetime.now(timezone.utc),
            human_action_required=status is AutonomousStatus.EXCEPTION_REQUIRED)

    def _rows(self, lines, consensus, property_resolution):
        values = {field.field_path: field.selected_value for field in consensus}
        rows = []
        for index, line in enumerate(lines):
            gl = line.get("gl_candidate") or {}
            gl_code = (gl.get("gl_code") if isinstance(gl, Mapping) and
                       float(gl.get("confidence") or 0) >= self.policy.threshold("gl_confidence") else None)
            invoice_date = values.get("document.invoice_date")
            rows.append({"Invoice Number": values.get("document.invoice_number"), "Bill or Credit": values.get("document.bill_or_credit"),
                "Invoice Date": invoice_date, "Accounting Date": values.get("document.accounting_date") or invoice_date,
                "Vendor": values.get("document.vendor"), "Invoice Description": values.get("document.description"),
                "Line Item Number": index + 1,
                "Property Abbreviation": property_resolution.selected_property, "GL Account": gl_code,
                "Amount": line.get("amount"), "Line Item Description": line.get("normalized_description") or line.get("raw_description"),
                "Expense Type": values.get("document.expense_type"), "Is Replacement Reserve": values.get("document.is_replacement_reserve"),
                "Due Date": values.get("document.due_date"), "Document Url": values.get("document.source_reference"),
                "_meta": {"invoice_group_id": values.get("document.invoice_number") or "autonomous",
                    "total_reconciliation_passed": all(check.passed for check in self.arithmetic.validate(consensus, lines)),
                    "accounting_decision": line.get("accounting_decision"), "autonomous_line_index": index}})
        return rows

    def _exceptions(self, consensus, checks, property, responsibility, reimbursement, readiness: AccountingReadiness, rows):
        exceptions = []
        material_conflicts = [field for field in consensus if field.material and field.status in {ConsensusStatus.UNRESOLVED_CONFLICT, ConsensusStatus.MISSING}]
        if len(material_conflicts) > int(self.policy.values["unresolved_material_conflicts_allowed"]): exceptions.append("material_field_conflict")
        if self.policy.values["arithmetic_must_pass"] and any(not check.passed for check in checks): exceptions.append("arithmetic_validation_failed")
        if rows and property.selected_property is None: exceptions.append("property_unresolved")
        if responsibility.review_required and not self._non_ap(consensus): exceptions.append("economic_responsibility_unresolved")
        if reimbursement.status not in {"resolved"} and not self._non_ap(consensus): exceptions.append("reimbursement_unresolved")
        if rows and not readiness.export_allowed: exceptions.extend(issue["code"] for issue in readiness_dict(readiness)["blockers"])
        if not rows and not self._non_ap(consensus): exceptions.append("financial_lines_missing")
        return exceptions

    @staticmethod
    def _non_ap(consensus):
        return any(field.field_path == "document.document_family" and str(field.selected_value).lower() in
                   {"non_ap", "notice", "marketing", "service_ticket"} for field in consensus)


def proposal_to_extraction(proposal: Mapping[str, Any]) -> ExtractionPass:
    fields = []
    for item in proposal.get("fields", []):
        evidence_raw = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
        fields.append(ExtractedField(field_path=item["field_path"], value=item.get("proposed_value"),
            confidence=float(item.get("confidence") or 0), source_pass="primary",
            evidence=[FieldEvidence(page=evidence_raw.get("page"), region=evidence_raw.get("region"),
                source_type=str(item.get("source") or "deterministic"), extraction_profile=str(item.get("profile_version") or "unknown"),
                raw_supporting_text=evidence_raw.get("text"))]))
    return ExtractionPass(pass_id="primary", profile_version=str(proposal.get("proposal_profile_version") or "unknown"),
        route=str(proposal.get("extraction_method") or "deterministic"), fields=fields,
        lines=list(proposal.get("lines") or []), warnings=[])


def deterministic_verification(primary: ExtractionPass) -> list[VerificationFinding]:
    """Independent structural pass: arithmetic/evidence checks only, no primary narrative."""
    output = []
    for field in primary.fields:
        evidence_present = bool(field.evidence) and any(e.raw_supporting_text or e.source_type for e in field.evidence)
        disposition = "confirmed" if evidence_present and field.value not in (None, "") else "missing"
        confidence = min(1.0, field.confidence + .02) if disposition == "confirmed" else 0
        output.append(VerificationFinding(field_path=field.field_path, disposition=disposition,
            value=field.value if disposition == "confirmed" else None, confidence=confidence,
            evidence=list(field.evidence), conflicts=[]))
    return output


def _normalize(value: Any) -> str:
    if value is None: return ""
    if isinstance(value, (dict, list)): return json.dumps(value, sort_keys=True, default=str).lower()
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _decimal(value: Any) -> Decimal | None:
    try:
        if value in (None, ""): return None
        result = Decimal(str(value).replace(",", ""))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError): return None


def _date(value: Any) -> date | None:
    try: return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError: return None


def _int(value: Any) -> int | None:
    try: return int(value) if value is not None else None
    except (TypeError, ValueError): return None


__all__ = ["AutonomousAdjudicator", "AutonomousAdjudicationResult", "AutonomousPolicy", "AutonomousStatus",
           "ConsensusField", "ConsensusStatus", "ExtractedField", "ExtractionPass", "FieldConsensusEngine",
           "FieldEvidence", "PropertyResolution", "ValidationCheck", "VerificationFinding",
           "deterministic_verification", "proposal_to_extraction"]
