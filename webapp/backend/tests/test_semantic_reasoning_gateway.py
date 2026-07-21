from decimal import Decimal

from webapp.backend.services.accounting_contracts import (
    GLAccountMetadata,
    LineItemFacts,
)
from webapp.backend.services import controlled_external_experiment
from webapp.backend.services import semantic_reasoning_gateway as gateway
from webapp.backend.services.gl_catalog import load_gl_catalog
from webapp.backend.services.semantic_classifier import classify_line
from webapp.backend.services.semantic_reasoning_gateway import (
    InvoiceSemanticLineRequest,
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


def test_controlled_external_semantics_are_local_candidate_only(monkeypatch):
    facts = LineItemFacts(
        line_item_id="line-local",
        raw_description="Bath tub refinishing",
        amount=Decimal("350"),
    )
    original = classify_line(facts, document_id="doc-local")
    local_account = GLAccountMetadata(
        gl_code="7001",
        gl_name="Synthetic Surface Service",
        gl_family="tub_refinishing",
        trade_families=["tub_refinishing"],
        compatible_work_modes=["labor_service"],
        incompatible_work_modes=["material_purchase"],
        payable=True,
        metadata_source="synthetic_test_catalog",
        metadata_confidence=1.0,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("controlled local semantics must not select or call a provider")

    monkeypatch.setattr(gateway, "controlled_external_active", lambda: True)
    monkeypatch.setattr(gateway, "_select_accounting_profile", forbidden)
    monkeypatch.setattr(gateway.ai_provider, "_send_chat_completion", forbidden)
    monkeypatch.setattr(gateway.ai_provider, "_experiment_reserve_attempt", forbidden)
    monkeypatch.setattr(
        controlled_external_experiment, "build_deepseek_minimized_facts", forbidden,
    )
    monkeypatch.setattr(gateway, "load_gl_catalog", lambda: (
        "synthetic-catalog/1.0", {"7001": local_account},
    ))

    result = gateway.enrich_invoice_semantics(
        lines=[InvoiceSemanticLineRequest(
            facts=facts,
            semantics=original,
            candidate_gl_codes=["7001"],
        )],
        document_id="doc-local",
        document_context="",
        tenant_id="exp-tenant-a",
    )["line-local"]

    assert result.trace == {
        "route": "controlled_local_semantics",
        "called": False,
        "provider": "local",
        "profile_id": None,
        "model_id": None,
        "estimated_cost_usd": 0.0,
        "canonical_concept": "surface.bathtub_refinishing",
        "version": "controlled-local-canonical-semantics/1.0",
        "authority": "candidate_only",
    }
    assert result.semantics.trade_family == "tub_refinishing"
    assert [candidate.gl_code for candidate in result.candidates] == ["7001"]
    assert result.candidates[0].source == "local_canonical_semantic_candidate"
    assert not hasattr(result, "selected_gl")
    assert not hasattr(result, "export_allowed")
