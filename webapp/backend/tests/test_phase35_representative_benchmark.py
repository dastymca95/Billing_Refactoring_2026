import json

import pytest
from pydantic import ValidationError

from webapp.backend.services.representative_benchmark import (
    AdjudicationStatus, BenchmarkLabel, DatasetEntry, EvaluationRecord, LabelStatus,
    LineLabel, ReviewerLabel, resolve_document, summarize,
)


def _review(reviewer: str, gl: str = "6540") -> ReviewerLabel:
    return ReviewerLabel(
        reviewer_id=reviewer, labeled_at="2026-07-14T00:00:00Z", label_confidence=.9,
        document_family="invoice", vendor_redacted="VENDOR-A", property_redacted="PROPERTY-A",
        invoice_number_redacted="INV-REDACTED", invoice_date="2026-01-01", total_amount="100.00",
        line_items=[LineLabel(line_item_id="line-1", raw_description_redacted="electrical service",
                              amount="100.00", line_family="labor_service", trade_family="electrical",
                              work_mode="labor_service", expected_gl=gl)],
        should_review=False, should_block=False,
    )


def test_gold_requires_independent_reviews_and_adjudication():
    with pytest.raises(ValidationError, match="gold labels require"):
        BenchmarkLabel(case_id="x", status=LabelStatus.GOLD, document_class="digital", known_vendor=True,
                       complexity="simple", value_cohort="normal", first_review=_review("one"),
                       adjudication_status=AdjudicationStatus.ADJUDICATED)
    gold = BenchmarkLabel(case_id="x", status=LabelStatus.GOLD, document_class="digital", known_vendor=True,
                          complexity="simple", value_cohort="normal", first_review=_review("one"),
                          second_review=_review("two"), adjudicated_gold=_review("adjudicator"),
                          adjudication_status=AdjudicationStatus.ADJUDICATED)
    assert gold.status is LabelStatus.GOLD


@pytest.mark.parametrize(
    "document_ref",
    [
        "../secret.pdf",
        r"C:\private\secret.pdf",
        "C:/private/secret.pdf",
        r"\\server\share\secret.pdf",
        "/private/secret.pdf",
        "file:///private/secret.pdf",
    ],
)
def test_private_document_references_cannot_escape_private_root(document_ref):
    with pytest.raises(ValidationError, match="safe relative"):
        DatasetEntry(case_id="x", document_class="scan", source="private", document_ref=document_ref,
                     label_ref="labels/x.json", private=True)


def test_private_documents_require_explicit_private_root(monkeypatch, tmp_path):
    monkeypatch.delenv("INNER_VIEW_PRIVATE_BENCHMARK_ROOT", raising=False)
    entry = DatasetEntry(case_id="x", document_class="scan", source="private",
                         document_ref="private/x.pdf", label_ref="labels/x.json", private=True)
    with pytest.raises(FileNotFoundError, match="INNER_VIEW_PRIVATE_BENCHMARK_ROOT"):
        resolve_document(entry, tmp_path)


def test_metrics_report_false_ready_high_confidence_errors_and_cohorts():
    bad = EvaluationRecord(
        "case-bad", "photo", False, "mixed", "high_value", "ai_vision", "ai_vision",
        {"vendor": True}, {"line_family:materials": True}, ("6675",), ("6500", "6675"),
        should_review=True, review_actual=False, should_block=True, blocked_actual=False, ready_actual=True,
        decision_confidence=.95, ai_calls=1, latency_ms=900, cost_usd=.05,
    )
    good = EvaluationRecord(
        "case-good", "digital", True, "simple", "normal", "deterministic", "deterministic",
        {"vendor": True}, {"line_family:labor_service": True}, ("6540",), ("6540",),
        should_review=False, review_actual=False, should_block=False, blocked_actual=False, ready_actual=True,
        decision_confidence=.8, ai_calls=0, latency_ms=100, cost_usd=0,
    )
    result = summarize([bad, good])
    assert result["gl_top_1"] == .5 and result["gl_top_3"] == 1
    assert result["false_ready_rate"] == .5
    assert result["route_accuracy"] == 1
    assert result["latency_ms_p50"] == 100 and result["latency_ms_p95"] == 900
    assert result["high_confidence_errors"][0]["case_id"] == "case-bad"
    assert result["cohorts"]["document_class"]["photo"]["documents"] == 1
