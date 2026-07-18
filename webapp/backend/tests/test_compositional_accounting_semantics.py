from decimal import Decimal

from webapp.backend.services.accounting_contracts import (
    DocumentFacts, EvidenceReference, GLAccountMetadata, LineItemFacts,
)
from webapp.backend.services.accounting_decision_engine import AccountingDecisionEngine
from webapp.backend.services.accounting_integration_bridges import ServiceReasoningCandidateAdapter
from webapp.backend.services.gl_catalog import load_gl_catalog
from webapp.backend.services.semantic_classifier import classify_line


def _classify_and_decide(text: str):
    line = LineItemFacts(
        line_item_id="line-1",
        raw_description=text,
        amount=Decimal("100.00"),
    )
    facts = DocumentFacts(
        document_id="generic-document",
        invoice_id="generic-invoice",
        line_items=[line],
        extraction_route="test",
    )
    semantics = classify_line(line, document_id=facts.document_id)
    _, catalog = load_gl_catalog()
    catalog = dict(catalog)
    catalog["6175"] = GLAccountMetadata(
        gl_code="6175", gl_name="TAA - License & Clicks",
        gl_family="license_click_service", trade_families=["license_click_service"],
        compatible_work_modes=["renewal", "recurring_service"], specificity="specific",
        payable=True, description_tokens=["license", "clicks", "taa"],
        scope_qualifiers=["taa"], metadata_source="chart_inference", metadata_confidence=.65,
    )
    candidates = ServiceReasoningCandidateAdapter().generate_candidates(
        line, semantics, catalog,
    )
    decision = AccountingDecisionEngine().decide(
        facts, semantics, catalog, candidates, {},
    )
    return semantics, decision


def test_membership_fee_is_classified_by_expense_subject_not_price_form():
    semantics, decision = _classify_and_decide(
        "Annual professional membership fee renewal",
    )
    assert semantics.line_family == "subscription_membership"
    assert semantics.work_mode == "renewal"
    assert decision.selected_gl_code == "6118"


def test_membership_package_renewal_uses_same_compositional_subject():
    semantics, decision = _classify_and_decide(
        "Apartment association membership package renewal for 80 locations",
    )
    assert semantics.line_family == "subscription_membership"
    assert decision.selected_gl_code == "6118"


def test_explicit_processing_fee_remains_a_fee_in_mixed_recurring_context():
    semantics, decision = _classify_and_decide(
        "Processing fee for annual subscription payment",
    )
    assert semantics.line_family == "fee"
    assert semantics.trade_family == "processing_fee"
    assert semantics.work_mode == "one_time_fee"
    assert decision.selected_gl_code != "6118"


def test_software_license_fee_uses_software_subject_not_generic_fee():
    semantics, decision = _classify_and_decide(
        "Annual hosted software license fee",
    )
    assert semantics.line_family == "subscription_membership"
    assert semantics.trade_family == "software_license"
    assert semantics.work_mode == "recurring_service"
    assert decision.selected_gl_code not in {"6173", "6188", "6956"}


def test_unqualified_fee_does_not_inherit_membership_semantics():
    semantics, _ = _classify_and_decide("Administrative fee")
    assert semantics.line_family == "fee"
    assert semantics.work_mode == "one_time_fee"


def test_line_scoped_section_context_produces_relevant_but_qualified_alternative():
    line = LineItemFacts(
        line_item_id="line-1",
        raw_description="Apartment package renewal for 80 locations",
        amount=Decimal("320.00"),
        evidence=[EvidenceReference(
            document_id="generic-document",
            text="Membership Type Click & Lease Tier 1",
            source_type="line_section_header",
            extraction_method="test",
        )],
    )
    facts = DocumentFacts(
        document_id="generic-document", invoice_id="generic-invoice",
        line_items=[line], extraction_route="test",
    )
    semantics = classify_line(line, document_id=facts.document_id)
    _, catalog = load_gl_catalog()
    catalog = dict(catalog)
    catalog["6175"] = GLAccountMetadata(
        gl_code="6175", gl_name="TAA - License & Clicks",
        gl_family="license_click_service", trade_families=["license_click_service"],
        compatible_work_modes=["renewal", "recurring_service"], specificity="specific",
        payable=True, description_tokens=["license", "clicks", "taa"],
        scope_qualifiers=["taa"], metadata_source="chart_inference", metadata_confidence=.65,
    )
    candidates = ServiceReasoningCandidateAdapter().generate_candidates(
        line, semantics, catalog,
    )
    decision = AccountingDecisionEngine().decide(
        facts, semantics, catalog, candidates, {},
    )
    assert semantics.trade_family == "license_click_service"
    assert "6175" in {candidate.gl_code for candidate in decision.candidates_ranked}
    assert decision.selected_gl_code == "6118"
    assert any(item.get("gl_code") == "6175" and "qualifier(s) TAA" in item.get("reason", "")
               for item in decision.rejected_alternatives)
