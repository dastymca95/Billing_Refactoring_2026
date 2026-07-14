from decimal import Decimal

import pytest
from pydantic import ValidationError

from webapp.backend.services.economic_responsibility import (
    AllocationScope, AllocationTarget, EconomicBearerType, EconomicResponsibility,
    EconomicResponsibilityClassifier, EvidenceStrength, FilenameFolderContextParser,
    LineResponsibility, PaymentSourceType, ResponsibilityEvidence, SettlementTreatment,
)


def evidence(*supports, strength=EvidenceStrength.STRONG, source="document_text", contradicts=()):
    return ResponsibilityEvidence(evidence_type="test", source=source, strength=strength,
                                  supports=list(supports), contradicts=list(contradicts), confidence=1)


def test_management_payment_for_single_property_is_reimbursable_only_with_combined_evidence():
    result = EconomicResponsibilityClassifier().classify("d", [
        evidence("payment_source:management_company_card"),
        evidence("economic_bearer:property"),
        evidence("allocation_scope:single_property"),
    ])
    assert result.payment_source is PaymentSourceType.MANAGEMENT_COMPANY_CARD
    assert result.settlement_treatment is SettlementTreatment.REIMBURSABLE_TO_MANAGEMENT_COMPANY
    assert result.review_required is False


def test_shipping_address_alone_does_not_infer_reimbursement():
    result = EconomicResponsibilityClassifier().classify("d", [
        evidence("economic_bearer:property", "allocation_scope:single_property",
                 strength=EvidenceStrength.MODERATE, source="shipping_address")])
    assert result.payment_source is PaymentSourceType.UNKNOWN
    assert result.settlement_treatment is SettlementTreatment.MANUAL_REVIEW
    assert result.review_required is True


def test_billed_entity_alone_does_not_force_corporate_expense():
    result = EconomicResponsibilityClassifier().classify("d", [
        evidence("economic_bearer:management_company", strength=EvidenceStrength.MODERATE, source="billed_entity")])
    assert result.settlement_treatment is SettlementTreatment.MANUAL_REVIEW


def test_property_payment_and_bearer_is_direct_expense():
    result = EconomicResponsibilityClassifier().classify("d", [
        evidence("payment_source:property_bank"), evidence("economic_bearer:property"),
        evidence("allocation_scope:single_property")])
    assert result.settlement_treatment is SettlementTreatment.PROPERTY_DIRECT_EXPENSE


def test_multiple_properties_requires_allocation_treatment():
    result = EconomicResponsibilityClassifier().classify("d", [
        evidence("economic_bearer:multiple_properties"), evidence("allocation_scope:multiple_properties")])
    assert result.settlement_treatment is SettlementTreatment.MULTI_PROPERTY_ALLOCATION


def test_mixed_line_items_override_document_level_treatment():
    lines = [
        LineResponsibility(line_item_id="1", economic_bearer="property", settlement_treatment="property_direct_expense",
                           allocation_scope="single_property"),
        LineResponsibility(line_item_id="2", economic_bearer="management_company", settlement_treatment="corporate_expense",
                           allocation_scope="corporate"),
    ]
    result = EconomicResponsibilityClassifier().classify("d", [], lines)
    assert result.settlement_treatment is SettlementTreatment.MIXED_LINE_LEVEL
    assert result.economic_bearer is EconomicBearerType.MIXED


def test_non_ap_requires_explicit_strong_evidence():
    result = EconomicResponsibilityClassifier().classify("d", [evidence("document_role:non_ap")])
    assert result.settlement_treatment is SettlementTreatment.NON_AP_DOCUMENT


def test_conflicting_evidence_requires_review():
    result = EconomicResponsibilityClassifier().classify("d", [
        evidence("economic_bearer:property"),
        evidence("economic_bearer:management_company"),
    ])
    assert result.economic_bearer is EconomicBearerType.UNKNOWN
    assert result.review_required


def test_allocation_percentages_must_sum_to_100():
    with pytest.raises(ValidationError, match="sum to 100"):
        EconomicResponsibility(document_id="d", payment_source="unknown", economic_bearer="multiple_properties",
            settlement_treatment="multi_property_allocation", allocation_scope="multiple_properties",
            allocation_targets=[AllocationTarget(target_type="property", target_reference="A", percentage=Decimal("40")),
                                AllocationTarget(target_type="property", target_reference="B", percentage=Decimal("40"))],
            review_required=False)


def test_filename_context_produces_non_authoritative_candidates_only():
    facts = FilenameFolderContextParser().parse("d", "purchase $123.45 unit 7.pdf", ["2026-04", "maintenance"])
    assert {candidate.candidate_type for candidate in facts.candidates} >= {"amount", "date", "unit_or_project", "expense_category"}
    assert all(candidate.authoritative is False for candidate in facts.candidates)
    assert all(item.source == "filename_or_folder_context" and item.strength is EvidenceStrength.WEAK
               for item in facts.to_evidence())


def test_filename_amount_conflicts_remain_candidates_not_truth():
    facts = FilenameFolderContextParser().parse("d", "receipt $10.00.pdf", ["batch $20.00"])
    assert "multiple_amount_candidates" in facts.warnings
    result = EconomicResponsibilityClassifier().classify("d", facts.to_evidence())
    assert result.settlement_treatment is SettlementTreatment.MANUAL_REVIEW


def test_gl_is_not_an_input_to_responsibility_classifier():
    parameters = EconomicResponsibilityClassifier.classify.__annotations__
    assert "gl" not in parameters and "selected_gl" not in parameters
