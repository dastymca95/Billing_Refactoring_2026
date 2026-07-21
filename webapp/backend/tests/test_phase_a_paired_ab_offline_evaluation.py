from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts.run_phase_a_paired_supplementary_ab import _evaluate
from webapp.backend.services.supplementary_ab_experiment import ExperimentArm


def _record():
    return SimpleNamespace(
        packet_id="synthetic-packet",
        packet_sha256="a" * 64,
        plan_id="synthetic-plan",
        target_category="total_mismatch",
        crops=(SimpleNamespace(
            crop_id="synthetic-crop",
            role="summary",
            ordinal=0,
            category="summary",
        ),),
    )


def _unresolved_payload() -> dict:
    return {
        "contract_version": "supplementary-transport/2.0",
        "target_type": "total_mismatch",
        "visibility_status": "not_visible",
        "unresolved_flag": True,
        "contradiction_flag": False,
        "page_number": None,
        "confidence": None,
        "raw_visible_text": {
            "value": None,
            "evidence_refs": [],
            "confidence": None,
            "visibility_status": "not_visible",
        },
        "observed_candidate_value": {
            "resolution_kind": "none",
            "field_name": None,
            "value": None,
            "source_page": None,
            "section_header": None,
            "row_label": None,
            "location_candidate": None,
            "activity": None,
            "raw_description": None,
            "quantity": None,
            "unit_price": None,
            "amount": None,
            "tax": None,
            "evidence_refs": [],
            "confidence": None,
            "visibility_status": "not_visible",
        },
        "observed_candidates": [],
        "financial_components": [],
        "visible_labels": [],
        "contradiction_observations": [],
        "warnings": ["synthetic_unresolved"],
    }


def _envelope(payload: dict) -> str:
    return json.dumps(payload)


def _evaluate_offline(raw_text: str):
    return _evaluate(
        record=_record(),
        arm=ExperimentArm.A,
        model_id="synthetic-offline-model",
        initial_facts={
            "line_items": [],
            "evidence": [{"source_type": "synthetic", "page": 1}],
            "needs_manual_review": True,
        },
        raw_text=raw_text,
        finish_reason="STOP",
        latency_ms=0.0,
        usage={"input_tokens": 0, "visible_output_tokens": 0},
        thinking_tokens=0,
        actual_cost=Decimal("0"),
        provider_schema_available=True,
    )


def test_offline_runner_persists_specific_safe_schema_diagnostics(monkeypatch):
    def forbidden_network(*args, **kwargs):
        raise AssertionError("offline evaluation must not call a provider")

    monkeypatch.setattr("urllib.request.urlopen", forbidden_network)
    payload = _unresolved_payload()
    del payload["visibility_status"]
    evaluation, observation = _evaluate_offline(_envelope(payload))
    assert observation is None
    assert evaluation["failure_code"] == "supplementary_required_field_missing"
    assert evaluation["safe_schema_diagnostics"]["failure_code"] == (
        "supplementary_required_field_missing"
    )
    assert evaluation["safe_schema_diagnostics"]["missing_required_fields"] == [
        "visibility_status"
    ]
    serialized = json.dumps(evaluation, sort_keys=True)
    assert "synthetic_unresolved" not in serialized
    assert evaluation["accepted"] is False
    assert evaluation["export_allowed"] is False


def test_offline_runner_preserves_valid_unresolved_provider_behavior(monkeypatch):
    def forbidden_network(*args, **kwargs):
        raise AssertionError("offline evaluation must not call a provider")

    monkeypatch.setattr("urllib.request.urlopen", forbidden_network)
    evaluation, observation = _evaluate_offline(_envelope(_unresolved_payload()))
    assert observation is not None
    assert evaluation["schema_valid"] is True
    assert evaluation["outcome"] == "unresolved"
    assert evaluation["final_disposition"] == "review_required"
    assert evaluation["accepted"] is False
    assert evaluation["export_allowed"] is False
    assert evaluation["false_safe_export"] is False
