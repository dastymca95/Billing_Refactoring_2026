from webapp.backend.services.canonical_semantics import (
    resolve_canonical_concept,
    semantic_candidate_cache_key,
)
from webapp.backend.services.accounting_contracts import LineItemFacts
from webapp.backend.services.semantic_classifier import classify_line


def _key(text: str):
    concept = resolve_canonical_concept(text)
    return semantic_candidate_cache_key(
        [concept],
        candidate_gl_codes=[["6512", "6570"]],
        provider="provider",
        profile_id="accounting",
        model_id="model",
        tenant_context_fingerprint="tenant-context",
        version="reasoner/1.0",
    )


def test_equivalent_literal_wording_has_same_candidate_cache_key():
    assert _key("Refinish kitchen counter in unit 22A") == _key(
        "Kitchen countertop refinishing for apartment 61G"
    )


def test_window_sill_and_tub_mat_are_distinct_canonical_concepts():
    sill = resolve_canonical_concept("3 Window Sills")
    mat = resolve_canonical_concept("1 Tub Mat")
    assert sill.concept_id == "surface.window_sill_refinishing"
    assert mat.concept_id == "surface.tub_mat_work"
    assert sill.concept_id != mat.concept_id


def test_unresolved_literal_is_not_reusable_semantic_cache_identity():
    assert _key("unreadable handwritten component") is None


def test_tenant_accounting_context_invalidates_candidate_cache():
    concept = resolve_canonical_concept("Bath Tub")
    base = dict(
        concepts=[concept], candidate_gl_codes=[["6570"]], provider="provider",
        profile_id="accounting", model_id="model", version="reasoner/1.0",
    )
    assert semantic_candidate_cache_key(
        tenant_context_fingerprint="tenant-a", **base
    ) != semantic_candidate_cache_key(
        tenant_context_fingerprint="tenant-b", **base
    )


def test_classifier_backfills_reviewed_vendor_neutral_concept_without_selecting_gl():
    result = classify_line(
        LineItemFacts(line_item_id="line-1", raw_description="3 Window Sills"),
        document_id="document-1",
    )
    assert result.line_family == "labor_service"
    assert result.trade_family == "tub_refinishing"
    assert result.work_mode == "labor_service"
    assert any(
        item.source_type == "canonical_semantic_concept"
        and item.normalized_text == "surface.window_sill_refinishing"
        for item in result.positive_evidence
    )
