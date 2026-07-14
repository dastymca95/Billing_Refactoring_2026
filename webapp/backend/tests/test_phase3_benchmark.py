import json

import pytest

from webapp.backend.services.document_benchmark import BenchmarkCase, CaseResult, compare, load_manifest, summarize


def test_manifest_rejects_non_sanitized_cases(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"cases": [{"case_id": "x", "document_class": "x", "document_path": "x",
                                           "expected_path": "x", "sanitized": False}]}))
    with pytest.raises(ValueError, match="sanitized"):
        load_manifest(path)


def test_summary_measures_accounting_quality_readiness_cost_and_latency():
    result = CaseResult("c", "deterministic", None, {"vendor": True, "amount": True}, 2, 1, 1,
                        false_ready=False, review_expected=True, review_actual=True,
                        processing_time_ms=100, estimated_cost_usd=0)
    summary = summarize([result])
    assert summary["field_accuracy"]["vendor"] == 1
    assert summary["gl_fill_rate"] == .5
    assert summary["blank_gl_rate"] == .5
    assert summary["false_ready_rate"] == 0
    assert summary["processing_time_ms_p50"] == 100


def test_comparison_has_directional_deltas():
    delta = compare({"gl_fill_rate": .5, "estimated_cost_usd": .1},
                    {"gl_fill_rate": .8, "estimated_cost_usd": .2})["delta"]
    assert delta["gl_fill_rate"] == pytest.approx(.3)
    assert delta["estimated_cost_usd"] == pytest.approx(.1)
