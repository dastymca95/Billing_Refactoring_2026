from __future__ import annotations

from decimal import Decimal

import pytest

from webapp.backend.services.accounting_contracts import DocumentFacts, GLCandidate, LineItemFacts
from webapp.backend.services.accounting_decision_engine import AccountingDecisionEngine
from webapp.backend.services.gl_catalog import load_gl_catalog
from webapp.backend.services.semantic_classifier import classify_line
from webapp.backend.services import row_normalizer
from webapp.backend.services.accounting_pipeline_v2 import _adapt_candidates
from webapp.backend.services.accounting_readiness import evaluate_rows


def decide(text: str, codes: list[tuple[str, str]], *, generated: str | None = None):
    line = LineItemFacts(line_item_id="line-1", raw_description=text,
        normalized_description=text.lower(), generated_description=generated, amount=Decimal("100"))
    facts = DocumentFacts(document_id="doc", invoice_id="inv", line_items=[line], extraction_route="gold_test")
    semantics = classify_line(line, document_id="doc")
    _, catalog = load_gl_catalog()
    candidates = [GLCandidate(gl_code=code, gl_name=catalog[code].gl_name, source=source,
        source_id="test", base_score=0.95, rule_version="test/1") for code, source in codes]
    return semantics, AccountingDecisionEngine().decide(facts, semantics, catalog, candidates, {})


@pytest.mark.parametrize("text,service_gl,supply_gl,expected_family,expected_mode", [
    ("Painting labor for unit 2", "6760", "6770", "painting", "labor_service"),
    ("Paint gallons primer and rollers", "6760", "6770", "painting", "material_purchase"),
    ("Apartment cleaning service", "6750", "6730", "cleaning", "labor_service"),
    ("Janitorial chemicals and cleaning supplies", "6750", "6730", "cleaning", "material_purchase"),
    ("Plumbing repair service for leaking faucet", "6565", "6675", "plumbing", "labor_service"),
    ("Plumbing fittings valves and parts", "6565", "6675", "plumbing", "material_purchase"),
    ("Electrical labor to repair outlet", "6540", "6627", "electrical", "labor_service"),
    ("Electrical breakers and wiring parts", "6540", "6627", "electrical", "material_purchase"),
])
def test_service_material_guardrails(text, service_gl, supply_gl, expected_family, expected_mode):
    semantics, decision = decide(text, [(service_gl, "deterministic_parser"), (supply_gl, "vendor_default")])
    assert semantics.trade_family == expected_family
    assert semantics.work_mode == expected_mode
    expected = service_gl if expected_mode == "labor_service" else supply_gl
    assert decision.selected_gl_code == expected
    assert any(c.gl_code != expected and c.negative_evidence for c in decision.candidates_ranked)


@pytest.mark.parametrize("text,code,line_family", [
    ("Annual membership renewal", "6118", "subscription_membership"),
    ("One time late fee", "6188", "fee"),
    ("Monthly water service usage", "6955", "utility"),
    ("Attorney legal service for eviction", "6205", "legal"),
    ("Annual insurance policy renewal", "7120", "insurance"),
])
def test_universal_non_trade_families(text, code, line_family):
    semantics, decision = decide(text, [(code, "deterministic_parser")])
    assert semantics.line_family == line_family
    assert decision.selected_gl_code == code


def test_specific_compatible_gl_beats_broad_gl():
    _, decision = decide("Painting labor service", [("6500", "vendor_default"), ("6760", "canonical_rule")])
    assert decision.selected_gl_code == "6760"


def test_vendor_default_and_history_cannot_beat_incompatible_line_evidence():
    _, decision = decide("Paint gallons primer rollers", [("6760", "vendor_default"), ("6760", "historical_mapping"), ("6770", "catalog_text_match")])
    assert decision.selected_gl_code == "6770"
    assert "6760:incompatible_work_mode" in decision.contradictions


def test_capital_requires_explicit_evidence():
    _, ordinary = decide("General maintenance service", [("7595", "vendor_default"), ("6530", "deterministic_parser")])
    assert ordinary.selected_gl_code == "6530"
    semantics, capital = decide("Full renovation remodel labor service", [("7595", "canonical_rule")])
    assert semantics.capital_context == "capital"
    assert capital.selected_gl_code == "7595"


def test_unknown_line_and_no_safe_candidate_block():
    semantics, decision = decide("miscellaneous charge", [("7120", "vendor_default")])
    assert semantics.trade_family == "unknown"
    assert decision.selected_gl_code is None
    assert decision.review_blocking


def test_non_payable_and_outside_chart_are_rejected():
    line = LineItemFacts(line_item_id="line-1", raw_description="Legal service", normalized_description="legal service")
    facts = DocumentFacts(document_id="doc", invoice_id="inv", line_items=[line], extraction_route="test")
    semantics = classify_line(line, document_id="doc")
    _, catalog = load_gl_catalog()
    candidates = [
        GLCandidate(gl_code="1100", gl_name="Undeposited Funds", source="legacy", base_score=.9),
        GLCandidate(gl_code="999999", gl_name="Outside", source="legacy", base_score=.9),
    ]
    decision = AccountingDecisionEngine().decide(facts, semantics, catalog, candidates, {})
    assert decision.selected_gl_code is None
    assert len(decision.rejected_alternatives) == 2


def test_generated_description_never_becomes_source_evidence():
    semantics, decision = decide("maintenance", [("6530", "deterministic_parser")], generated="Painting labor service")
    assert semantics.trade_family == "general_maintenance"
    assert all("Painting labor service" not in str(item) for item in decision.evidence)


def test_same_semantics_across_vendor_names_is_stable():
    first_semantics, first = decide("Plumbing repair service", [("6565", "vendor_default")])
    second_semantics, second = decide("Plumbing repair service", [("6565", "historical_mapping")])
    assert first_semantics.trade_family == second_semantics.trade_family
    assert first.selected_gl_code == second.selected_gl_code == "6565"


def test_source_line_preservation_and_location_extraction():
    raw = "Unit 204 Painting labor"
    semantics, decision = decide(raw, [("6760", "deterministic_parser")], generated="Painted Apartment 204")
    assert semantics.location_detected == "204"
    assert decision.evidence[0]["text"] == raw


def test_top_two_ambiguity_requires_review():
    # Two equally specific, compatible legal accounts intentionally create a close margin.
    _, decision = decide("Legal attorney service", [("8050", "deterministic_parser"), ("6205", "deterministic_parser")])
    assert decision.review_required


def test_row_adapter_preserves_raw_normalized_and_generated_descriptions():
    row = {
        "Invoice Number": "INV-1", "Vendor": "Vendor", "Property Abbreviation": "PROP",
        "GL Account": "6530", "Invoice Description": "Governor Repair; Trip Charge",
        "Line Item Number": 2, "Line Item Description": "Trip charge", "Amount": 95,
        "_meta": {"source_file": "invoice.pdf", "source_line_description": "Trip charge"},
    }
    row_normalizer.normalize_rows([row], batch_id="batch", source_file="invoice.pdf")
    source = row["_meta"]["source_text"]
    assert source["raw_description"] == "Trip charge"
    assert row["_meta"]["normalized_source_description"] == "Trip Charge"
    assert row["_meta"]["generated_line_description"] == row["Line Item Description"]
    assert source["raw_description"] != row["Invoice Description"]


def test_vague_maintenance_does_not_select_unrelated_alternative():
    semantics, decision = decide("General maintenance", [("6530", "deterministic_parser"), ("7120", "historical_mapping")])
    assert semantics.trade_family == "general_maintenance"
    assert decision.selected_gl_code == "6530"
    assert all(candidate.gl_code != "7120" for candidate in decision.candidates_ranked)


def test_mixed_material_and_labor_is_explicit_and_reviewable():
    semantics, decision = decide("Painting labor installation plus paint materials and rollers", [("6760", "canonical_rule"), ("6770", "canonical_rule")])
    assert "mixed_material_and_service_indicators" in semantics.contradictions
    assert decision.selected_gl_code in {"6760", "6770"}
    assert decision.review_required


@pytest.mark.parametrize("text,code", [
    ("Utility connection fee", "6956"),
    ("One time late fee", "6188"),
])
def test_utility_connection_and_late_fees(text, code):
    semantics, decision = decide(text, [(code, "utility_rule")])
    assert semantics.line_family == "fee"
    assert decision.selected_gl_code == code


def test_readiness_blocks_a_null_engine_selection():
    semantics, decision = decide("unclassified miscellaneous", [("1100", "historical_mapping")])
    assert decision.selected_gl_code is None
    row = {
        "Invoice Number": "INV", "Vendor": "Vendor", "Property Abbreviation": "PROP",
        "GL Account": decision.selected_gl_code or "", "Amount": 10,
        "_meta": {"accounting_decision": decision.model_dump(mode="json"), "invoice_group_id": "INV"},
    }
    readiness = evaluate_rows([row])
    assert not readiness.export_allowed
    assert any(issue.field == "GL Account" for issue in readiness.blockers)


def test_every_legacy_gl_source_is_adapted_to_typed_candidates():
    row = {
        "GL Account": "6760",
        "_meta": {"gl_candidate_inputs": [
            {"gl_code": "6770", "source": "ai_candidate"},
            {"gl_code": "6500", "source": "vendor_default"},
            {"gl_code": "6530", "source": "historical_mapping"},
            {"gl_code": "6615", "source": "learned_correction"},
            {"gl_code": "6750", "source": "canonical_rule"},
            {"gl_code": "6955", "source": "utility_rule"},
            {"gl_code": "6565", "source": "service_reasoner"},
            {"gl_code": "6540", "source": "manual_approved_rule"},
        ]},
    }
    candidates = _adapt_candidates(row, "painting labor service")
    sources = {candidate.source for candidate in candidates}
    assert {"deterministic_parser", "ai_candidate", "vendor_default", "historical_mapping",
            "learned_correction", "canonical_rule", "utility_rule", "service_reasoner",
            "manual_approved_rule"} <= sources


def test_complete_canonical_fixtures_pass_through_central_decision_engine():
    from webapp.backend.services.canonical_invoice_fixtures import run_all_complete

    result = run_all_complete()
    assert result["ok"]
    decisions = []
    for fixture in result["results"]:
        if fixture.get("skipped"):
            continue
        for row in fixture.get("rows") or []:
            meta = row.get("_meta") or {}
            decision = meta.get("accounting_decision") or {}
            shadow = meta.get("gl_shadow_comparison") or {}
            decisions.append(decision)
            assert decision.get("decision_source") == "AccountingDecisionEngine"
            assert decision.get("selected_gl_code") == row.get("GL Account")
            assert decision.get("review_blocking") is False
            assert shadow.get("same") is True
    assert decisions
