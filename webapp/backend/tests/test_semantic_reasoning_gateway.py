from decimal import Decimal

from webapp.backend.services.accounting_contracts import LineItemFacts
from webapp.backend.services.gl_catalog import load_gl_catalog
from webapp.backend.services.semantic_classifier import classify_line
from webapp.backend.services.semantic_reasoning_gateway import (
    SemanticReasoningProposal,
    _validate_and_adapt,
)


def test_ai_semantic_proposal_is_candidate_only_and_source_grounded():
    facts = LineItemFacts(line_item_id="line-1", raw_description="SKU QX91Z77, 5 GAL, color white",
                          quantity=Decimal("4"), unit_price=Decimal("20"), amount=Decimal("80"))
    original = classify_line(LineItemFacts(line_item_id="line-1", raw_description="QX91Z77"), document_id="doc")
    proposal = SemanticReasoningProposal(line_family="materials", trade_family="painting",
        work_mode="material_purchase", confidence=.88, evidence_quotes=["5 GAL", "color white"],
        candidate_gl_codes=["6770", "1100", "9999"],
        reasoning_summary="Physical paint merchandise, not performed labor.")
    _, catalog = load_gl_catalog()
    resolved, candidates = _validate_and_adapt(proposal, original, facts, "doc", facts.raw_description or "", "", catalog)
    assert resolved.line_family == "materials"
    assert resolved.trade_family == "painting"
    assert resolved.work_mode == "material_purchase"
    assert [candidate.gl_code for candidate in candidates] == ["6770"]
    assert all(candidate.source == "ai_semantic_reasoning_candidate" for candidate in candidates)


def test_ai_semantic_proposal_cannot_use_generated_or_invented_quote():
    facts = LineItemFacts(line_item_id="line-1", raw_description="Opaque SKU QX91Z77",
                          generated_description="Invented paint materials")
    original = classify_line(facts, document_id="doc")
    proposal = SemanticReasoningProposal(line_family="materials", trade_family="painting",
        work_mode="material_purchase", confidence=.9, evidence_quotes=["Invented paint materials"],
        candidate_gl_codes=["6770"], reasoning_summary="Candidate only")
    _, catalog = load_gl_catalog()
    try:
        _validate_and_adapt(proposal, original, facts, "doc", facts.raw_description or "", "", catalog)
    except ValueError as exc:
        assert str(exc) == "source_grounding_missing"
    else:
        raise AssertionError("invented evidence must be rejected")


def test_no_safe_decision_escalation_may_correct_known_semantics_but_remains_candidate_only():
    facts = LineItemFacts(
        line_item_id="line-1",
        raw_description="Annual membership fee renewal",
        amount=Decimal("100"),
    )
    original = classify_line(
        LineItemFacts(line_item_id="line-1", raw_description="Administrative fee"),
        document_id="doc",
    )
    proposal = SemanticReasoningProposal(
        line_family="subscription_membership",
        trade_family="admin",
        work_mode="renewal",
        confidence=.91,
        evidence_quotes=["Annual membership fee renewal"],
        candidate_gl_codes=["6118"],
        reasoning_summary="Membership is the expense subject; fee is the price form.",
    )
    _, catalog = load_gl_catalog()
    resolved, candidates = _validate_and_adapt(
        proposal, original, facts, "doc", facts.raw_description or "", "", catalog,
        replace_existing=True,
    )
    assert resolved.line_family == "subscription_membership"
    assert resolved.work_mode == "renewal"
    assert [candidate.gl_code for candidate in candidates] == ["6118"]
    assert all(candidate.source == "ai_semantic_reasoning_candidate" for candidate in candidates)
