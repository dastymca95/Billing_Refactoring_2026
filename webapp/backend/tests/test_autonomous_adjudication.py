from decimal import Decimal

import pytest

from webapp.backend.services.autonomous_adjudication import (
    ArithmeticValidator, AutonomousAdjudicator, AutonomousPolicy, AutonomousStatus,
    ConsensusStatus, ExtractedField, ExtractionPass, FieldConsensusEngine, FieldEvidence,
    PropertyResolver, ReimbursementResolver, VerificationFinding, VisualPreprocessor, deterministic_verification,
)
from webapp.backend.services.economic_responsibility import EvidenceStrength, ResponsibilityEvidence
from webapp.backend.services.model_registry import ModelRegistry, ModelRole, ModelSpec
from webapp.backend.services.assisted_labeling import AssistedLabelingService
from webapp.backend.services.autonomous_private_runner import AutonomousPrivateRunner
from webapp.backend.services.reviewer_1_pilot import Reviewer1Pilot
from webapp.backend.tests.test_reviewer_1_pilot import pilot


EVIDENCE = [FieldEvidence(page=1, region=[0, 0, 1, 1], source_type="document_text",
                          extraction_profile="test/1.0", raw_supporting_text="visible value")]


def field(path, value, confidence=.95):
    return ExtractedField(field_path=path, value=value, confidence=confidence,
                          evidence=EVIDENCE, source_pass="primary")


def policy():
    return AutonomousPolicy({"required_field_confidence": .9, "property_confidence": .92,
        "responsibility_confidence": .9, "gl_confidence": .88, "supported_single_source_confidence": .95,
        "arithmetic_tolerance": .01, "arithmetic_must_pass": True,
        "unresolved_material_conflicts_allowed": 0, "independent_model_voting_max_calls": 2,
        "strong_reasoner_shadow_only": True}, "test-policy/1.0")


def coherent_primary():
    values = {"document.document_family": "invoice", "document.vendor": "Vendor",
        "document.invoice_number": "I-1", "document.invoice_date": "2026-01-01",
        "document.due_date": "2026-02-01", "document.total": "10.00",
        "document.property": "PROP", "document.bill_or_credit": "Bill",
        "document.description": "Service invoice", "document.expense_type": "Operating",
        "document.is_replacement_reserve": "False", "document.source_reference": "private-source-reference"}
    line = {"raw_description": "plumbing repair", "normalized_description": "plumbing repair",
            "amount": "10.00", "semantic_classification": {"line_family": "labor_service"},
            "gl_candidate": {"gl_code": "6565", "gl_name": "Plumbing Contract",
                             "source": "AccountingDecisionEngine", "confidence": .95, "status": "unverified"}}
    return ExtractionPass(pass_id="primary", profile_version="test/1.0", route="deterministic",
                          fields=[field(k, v) for k, v in values.items()], lines=[line])


def responsibility_evidence():
    return [ResponsibilityEvidence(evidence_type="coherent_test", page=1, source="document",
        strength=EvidenceStrength.AUTHORITATIVE, confidence=1,
        supports=["payment_source:property_card", "economic_bearer:property",
                  "allocation_scope:single_property", "settlement_treatment:property_direct_expense"])]


def test_consensus_distinguishes_exact_normalized_single_and_unresolved():
    engine = FieldConsensusEngine(policy())
    primary = [field("document.vendor", "Vendor, Inc."), field("document.total", "10.00"),
               field("document.invoice_number", "A-1", .96)]
    verification = [VerificationFinding(field_path="document.vendor", disposition="confirmed", value="vendor inc",
        confidence=.96, evidence=EVIDENCE), VerificationFinding(field_path="document.total", disposition="corrected",
        value="11.00", confidence=.7, evidence=EVIDENCE)]
    result = {item.field_path: item for item in engine.decide(primary, verification)}
    assert result["document.vendor"].status is ConsensusStatus.NORMALIZED_AGREEMENT
    assert result["document.total"].status is ConsensusStatus.UNRESOLVED_CONFLICT
    assert result["document.invoice_number"].status is ConsensusStatus.SUPPORTED_SINGLE_SOURCE


def test_arithmetic_failures_cannot_be_overridden():
    consensus = FieldConsensusEngine(policy()).decide([field("document.total", "12.00")],
        [VerificationFinding(field_path="document.total", disposition="confirmed", value="12.00", confidence=.96, evidence=EVIDENCE)])
    checks = ArithmeticValidator(Decimal("0.01")).validate(consensus, [{"amount": "10.00"}])
    assert checks and checks[0].passed is False


def test_structural_validation_covers_pages_period_credit_and_duplicates():
    primary = [field("document.total", "10.00"), field("document.bill_or_credit", "credit"),
        field("document.service_period_start", "2026-02-01"), field("document.service_period_end", "2026-01-01"),
        field("document.page_count", 2), field("document.page_numbers", [1]),
        field("document.duplicate_pages", [2]), field("document.repeated_line_items_unresolved", True),
        field("document.account_numbers", ["A-1", "B-2"]), field("document.discount", "-1.00")]
    verification = [VerificationFinding(field_path=f.field_path, disposition="confirmed", value=f.value,
        confidence=.96, evidence=EVIDENCE) for f in primary]
    consensus = FieldConsensusEngine(policy()).decide(primary, verification)
    failed = {check.code for check in ArithmeticValidator(Decimal("0.01")).validate(consensus, []) if not check.passed}
    assert {"credit_vs_charge_sign", "service_period_validity", "multi_page_continuity",
            "duplicate_pages", "repeated_line_items"} <= failed
    assert {"account_number_consistency", "discount_treatment"} <= failed


def test_property_conflict_remains_unresolved():
    consensus = FieldConsensusEngine(policy()).decide([field("document.property", "ONE")],
        [VerificationFinding(field_path="document.property", disposition="corrected", value="TWO", confidence=.7, evidence=EVIDENCE)])
    result = PropertyResolver(policy()).resolve(consensus)
    assert result.selected_property is None and result.contradiction_status != "none"


def test_configured_property_aliases_resolve_equivalent_evidence():
    primary = [field("document.property", "Property North"), field("document.filename_property", "PN")]
    verification = [VerificationFinding(field_path=f.field_path, disposition="confirmed", value=f.value,
        confidence=.96, evidence=EVIDENCE) for f in primary]
    consensus = FieldConsensusEngine(policy()).decide(primary, verification)
    result = PropertyResolver(policy(), {"PN": "Property North"}).resolve(consensus)
    assert result.selected_property == "Property North" and result.contradiction_status == "none"


def test_reimbursement_requires_entities_when_treatment_is_reimbursable():
    evidence = [ResponsibilityEvidence(evidence_type="r", source="document", strength=EvidenceStrength.AUTHORITATIVE,
        confidence=1, supports=["payment_source:management_company_card", "economic_bearer:property",
        "allocation_scope:single_property", "settlement_treatment:reimbursable_to_management_company"])]
    from webapp.backend.services.economic_responsibility import EconomicResponsibilityClassifier
    responsibility = EconomicResponsibilityClassifier().classify("d", evidence)
    result = ReimbursementResolver(policy()).resolve([], responsibility)
    assert result.reimbursement_required is True and result.status == "unresolved_entities"


def test_visual_preprocessing_is_read_only_and_detects_rotation(tmp_path):
    source = tmp_path / "receipt.jpg"; source.write_bytes(b"image bytes")
    before = source.read_bytes(); result = VisualPreprocessor().preprocess(source, rotation_degrees=90, handwriting_hint=True)
    assert result.source_type == "image" and result.rotation_degrees == 90
    assert result.handwriting_route_required is True and source.read_bytes() == before


def test_unadvertised_model_is_never_called_and_result_fails_closed(monkeypatch):
    calls = []
    registry = ModelRegistry([ModelSpec("vision-x", "test", frozenset({ModelRole.EXTRACTION_VISION}), supports_vision=True)])
    adjudicator = AutonomousAdjudicator(policy=policy(), registry=registry, advertised_models=[],
        extraction_gateway=lambda **kwargs: calls.append(kwargs))
    primary = coherent_primary(); primary.lines[0]["gl_candidate"] = None
    result = adjudicator.adjudicate("d", deterministic_primary=primary,
        deterministic_verification=deterministic_verification(primary), visual_required=True,
        responsibility_evidence=responsibility_evidence())
    assert not calls
    assert result.status is AutonomousStatus.EXCEPTION_REQUIRED
    assert any("GL Account" in code or code == "gl_invalid" for code in result.exception_codes)


def test_advertised_capability_uses_isolated_verification_gateway():
    seen = {}
    registry = ModelRegistry([ModelSpec("vision-x", "test", frozenset({ModelRole.EXTRACTION_VISION}), supports_vision=True)])
    primary = coherent_primary(); verification = deterministic_verification(primary)
    def gateway(**kwargs): seen.update(kwargs); return primary, verification
    result = AutonomousAdjudicator(policy=policy(), registry=registry, advertised_models=["vision-x"],
        extraction_gateway=gateway).adjudicate("d", deterministic_primary=primary,
            deterministic_verification=[], visual_required=True, responsibility_evidence=responsibility_evidence())
    assert seen["isolated_verification"] is True and seen["max_model_calls"] == 2
    assert result.gold_status == "not_gold"


def test_coherent_financial_document_can_machine_adjudicate(monkeypatch):
    primary = coherent_primary()
    result = AutonomousAdjudicator(policy=policy(), registry=ModelRegistry([]), advertised_models=[]).adjudicate(
        "d", deterministic_primary=primary, deterministic_verification=deterministic_verification(primary),
        visual_required=False, responsibility_evidence=responsibility_evidence())
    assert result.status is AutonomousStatus.MACHINE_ADJUDICATED
    assert result.accounting_readiness["export_allowed"] is True
    assert result.human_action_required is False and result.gold_status == "not_gold"


def test_material_conflict_requires_exception_even_with_other_agreement():
    primary = coherent_primary(); verification = deterministic_verification(primary)
    total = next(item for item in verification if item.field_path == "document.total")
    total.disposition = "corrected"; total.value = "99.00"; total.confidence = .7
    result = AutonomousAdjudicator(policy=policy(), registry=ModelRegistry([]), advertised_models=[]).adjudicate(
        "d", deterministic_primary=primary, deterministic_verification=verification,
        visual_required=False, responsibility_evidence=responsibility_evidence())
    assert result.status is AutonomousStatus.EXCEPTION_REQUIRED
    assert "material_field_conflict" in result.exception_codes


def test_private_runner_writes_analysis_only_and_preserves_labels_and_hash(tmp_path):
    workspace, pilot_controller = pilot(tmp_path)
    frozen = workspace.selection_dir / "selected_120_v1.json"; frozen.write_bytes(workspace.selected_path.read_bytes())
    before_hash = __import__("hashlib").sha256(frozen.read_bytes()).hexdigest()
    before_labels = {path.name: path.read_bytes() for path in workspace.labels_dir.glob("*.json")}
    runner = AutonomousPrivateRunner(workspace, pilot_controller,
        AssistedLabelingService(workspace, pilot_controller),
        AutonomousAdjudicator(policy=policy(), registry=ModelRegistry([]), advertised_models=[]))
    summary = runner.run_pilot()
    assert summary["documents"] == 20 and summary["machine_gold_count"] == 0
    assert len(list(runner.output_dir.glob("*.json"))) == 20
    assert __import__("hashlib").sha256(frozen.read_bytes()).hexdigest() == before_hash
    assert {path.name: path.read_bytes() for path in workspace.labels_dir.glob("*.json")} == before_labels
