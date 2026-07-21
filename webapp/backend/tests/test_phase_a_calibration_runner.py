from __future__ import annotations

import json
from pathlib import Path

import pytest

from webapp.backend.services.experiment_spend_controller import ExperimentSpendController
from webapp.backend.services.phase_a_calibration_runner import (
    PhaseATerminalDisposition,
    PrivateProviderTransferAuthorizationRequired,
    _configure_isolated_environment,
    _derive_terminal_disposition,
    _evaluate_frozen_baseline,
    _finalize_controlled_gate_metrics,
    _finalize_controlled_processor_result,
    _local_trace_metrics,
    _match_labeled_invoice,
    _rows_reconcile,
    run_phase_a_baseline,
)


def _row(gl_code: str = "7001") -> dict:
    return {
        "Invoice Number": "INV-100",
        "Invoice Total": "25.00",
        "Invoice Date": "2026-01-01",
        "Vendor": "Synthetic Vendor",
        "Property": "P1",
        "GL Account": gl_code,
        "Line Item Total": "25.00",
        "_meta": {
            "source_text": {"raw_description": "Synthetic service"},
            "document_facts": {
                "evidence": [{"source_type": "synthetic"}],
                "line_items": [],
            },
            "accounting_decision": {
                "candidates_ranked": [
                    {"gl_code": "7001"}, {"gl_code": "7002"},
                ],
            },
        },
    }


def test_phase_a_requires_explicit_private_provider_transfer_authorization(tmp_path: Path):
    with pytest.raises(PrivateProviderTransferAuthorizationRequired):
        run_phase_a_baseline(
            project_root=tmp_path / "not-resolved",
            source_root=tmp_path / "not-resolved",
            experiment_runtime_root=tmp_path / "not-resolved",
            inventory_snapshot_root=tmp_path / "not-resolved",
            calibration_manifest_path=tmp_path / "not-resolved",
            split_root=tmp_path / "not-resolved",
            experiment_id="exp-test",
        )
    assert not (tmp_path / "not-resolved").exists()


def test_local_phase_a_requires_model_but_not_external_transfer_authorization(tmp_path: Path):
    with pytest.raises(ValueError, match="local_model_required"):
        run_phase_a_baseline(
            project_root=tmp_path / "not-resolved",
            source_root=tmp_path / "not-resolved",
            experiment_runtime_root=tmp_path / "not-resolved",
            inventory_snapshot_root=tmp_path / "not-resolved",
            calibration_manifest_path=tmp_path / "not-resolved",
            split_root=tmp_path / "not-resolved",
            experiment_id="exp-test",
            local_only=True,
        )


def test_local_phase_a_environment_is_loopback_only(tmp_path: Path, monkeypatch):
    for key in (
        "INNER_VIEW_EXPERIMENT_MODE", "INNER_VIEW_EXPERIMENT_EXECUTION_MODE",
        "INNER_VIEW_WEBAPP_DATA_ROOT",
        "INNER_VIEW_TENANT_ID", "INNER_VIEW_DEPLOYMENT_MODE",
        "INNER_VIEW_LOCAL_INFERENCE_ONLY", "LOCAL_MULTIMODAL_MODEL",
        "LOCAL_MULTIMODAL_BASE_URL", "LOCAL_MULTIMODAL_PROFILE_ID",
        "LOCAL_MULTIMODAL_TIMEOUT_SECONDS", "LOCAL_MULTIMODAL_CONTEXT_TOKENS",
        "AI_ASSIST_ENABLED", "AI_PROVIDER", "AI_MODEL", "AI_API_KEY",
        "AI_BASE_URL", "AI_VISION_ENABLED", "AI_VISION_PROVIDER",
        "AI_VISION_MODEL", "AI_VISION_API_KEY", "AI_VISION_BASE_URL",
        "AI_VISION_MODE", "AI_VISION_NATIVE_PDF_ENABLED",
        "AI_FAST_FIRST_FACTS_ONLY_ENABLED", "AI_FAST_FIRST_GOLDEN_PARITY_APPROVED",
        "AI_SEMANTIC_REASONING_ENABLED", "AI_MAX_COST_PER_BATCH_USD",
        "AI_FILE_WORKERS", "AI_INVOICE_GROUP_WORKERS",
        "ACCOUNTING_DECISION_ENGINE_V2",
    ):
        monkeypatch.setenv(key, "")
    _configure_isolated_environment(
        tmp_path, "exp-test", local_only=True,
        local_model="qwen3-vl:2b-instruct",
        local_base_url="http://127.0.0.1:11434",
        local_profile_id="local-qwen3-vl-2b-instruct",
    )
    assert __import__("os").environ["INNER_VIEW_LOCAL_INFERENCE_ONLY"] == "1"
    assert __import__("os").environ["AI_PROVIDER"] == "local_ollama"
    assert __import__("os").environ["AI_BASE_URL"] == "http://127.0.0.1:11434"
    assert __import__("os").environ["AI_VISION_NATIVE_PDF_ENABLED"] == "0"
    assert __import__("os").environ["LOCAL_MULTIMODAL_PROFILE_ID"] == "local-qwen3-vl-2b-instruct"
    assert __import__("os").environ["AI_FILE_WORKERS"] == "1"
    assert __import__("os").environ["AI_INVOICE_GROUP_WORKERS"] == "1"


def test_controlled_external_environment_is_gemini_facts_only(tmp_path: Path, monkeypatch):
    from webapp.backend.services.controlled_external_experiment import ExperimentExecutionMode

    for key in (
        "INNER_VIEW_EXPERIMENT_EXECUTION_MODE",
        "INNER_VIEW_LOCAL_INFERENCE_ONLY", "AI_VISION_MODE",
        "AI_FAST_FIRST_FACTS_ONLY_ENABLED", "AI_FAST_FIRST_GOLDEN_PARITY_APPROVED",
        "AI_SEMANTIC_REASONING_ENABLED", "AI_TEXT_ROUTING_PROFILE_ID",
    ):
        monkeypatch.setenv(key, "")
    _configure_isolated_environment(
        tmp_path, "exp-test", execution_mode=ExperimentExecutionMode.CONTROLLED_EXTERNAL,
    )
    env = __import__("os").environ
    assert env["INNER_VIEW_LOCAL_INFERENCE_ONLY"] == "0"
    assert env["AI_VISION_MODE"] == "always"
    assert env["AI_FAST_FIRST_FACTS_ONLY_ENABLED"] == "1"
    assert env["AI_FAST_FIRST_GOLDEN_PARITY_APPROVED"] == "1"
    assert env["AI_SEMANTIC_REASONING_ENABLED"] == "0"
    assert env["AI_TEXT_ROUTING_PROFILE_ID"] == ""


def test_local_trace_metrics_separate_remote_attempts(tmp_path: Path):
    trace = tmp_path / "batches" / "batch_safe" / "audit" / "ai_request_trace.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text("\n".join([
        json.dumps({"event": "provider_attempt", "provider": "local_ollama",
                    "elapsed_ms": 10, "provider_peak_concurrency": 1}),
        json.dumps({"event": "provider_attempt", "provider": "openai",
                    "elapsed_ms": 20}),
        json.dumps({"event": "network_dispatch_blocked", "provider": "gemini"}),
    ]) + "\n", encoding="utf-8")
    metrics = _local_trace_metrics(tmp_path)
    assert metrics["local_provider_calls"] == 1
    assert metrics["remote_provider_calls"] == 1
    assert metrics["blocked_network_attempts"] == 1


def test_rows_reconcile_requires_observed_totals():
    assert _rows_reconcile([_row()]) is True
    broken = _row()
    broken["Line Item Total"] = "24.00"
    assert _rows_reconcile([broken]) is False
    missing = _row()
    missing["Invoice Total"] = ""
    assert _rows_reconcile([missing]) is False


def test_labeled_invoice_match_refuses_ambiguous_outputs():
    invoice = {"rows": [_row()]}
    label = {"observed_invoice_number": "INV-100", "observed_invoice_total": "25.00"}
    assert _match_labeled_invoice([invoice], label) == invoice
    assert _match_labeled_invoice([invoice, invoice], label) is None


def test_frozen_baseline_evaluator_uses_hidden_labels_only_after_freeze(tmp_path: Path):
    split = tmp_path / "split"
    (split / "scopes").mkdir(parents=True)
    (split / "hidden").mkdir()
    label = {
        "unit_id": "unit-one",
        "representative_document_id": "doc-one",
        "ground_truth": {
            "observed_invoice_number": "INV-100",
            "observed_invoice_total": "25.00",
            "expected_gl": "7001",
            "acceptable_gl_alternatives": [],
            "expected_property": "P1",
        },
    }
    (split / "scopes" / "training_labels.jsonl").write_text(
        json.dumps(label) + "\n", encoding="utf-8",
    )
    for name in ("benchmark_only_labels.jsonl", "rule_simulation_labels.jsonl"):
        (split / "scopes" / name).write_text("", encoding="utf-8")
    (split / "hidden" / "holdout_labels.jsonl").write_text("", encoding="utf-8")
    frozen = tmp_path / "frozen.json"
    frozen.write_text(json.dumps({
        "source_map": {
            "safe.pdf": {
                "unit_id": "unit-one", "document_id": "doc-one",
            },
        },
        "shard_results": [{
            "all_invoices": [{
                "source_file": "safe.pdf",
                "rows": [_row()],
                "_experiment_readiness": {"export_allowed": True},
            }],
            "unsupported_files": [],
        }],
        "shard_failures": [],
    }), encoding="utf-8")
    import hashlib
    digest = hashlib.sha256(frozen.read_bytes()).hexdigest()
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    metrics = _evaluate_frozen_baseline(
        frozen_path=frozen,
        expected_frozen_sha256=digest,
        split_root=split,
        controller=controller,
        wall_seconds=1.0,
    )
    assert metrics["top1_accuracy"] == 1.0
    assert metrics["top3_recall"] == 1.0
    assert metrics["false_safe_export_rate"] == 0.0
    assert metrics["provider_calls"] == 0


def _controlled_failure_payload(kind: str) -> dict:
    payload = {
        "success": False,
        "summary": {
            "processing_mode": "ai_assisted",
            "files_total": 1,
            "files_unsupported": 1,
            "processing_failures": 1,
            "invoices_produced": 0,
            "safe_terminal_stage": "normalization",
        },
        "invoices": [],
        "manual_review_rows": [],
        "unsupported_files": [],
    }
    if kind == "review":
        payload["manual_review_rows"] = [{
            "reason_codes": ["total_reconciliation_failed"],
            "message": "PRIVATE-VENDOR PRIVATE-AMOUNT PRIVATE-FILENAME.pdf",
        }]
        payload["supplementary_provenance"] = {"evidence_reference_count": 1}
    elif kind == "unsupported":
        payload["unsupported_files"] = [{
            "reason": "ai_response_invalid_json",
            "filename": "PRIVATE-FILENAME.pdf",
            "message": "PRIVATE-VENDOR PRIVATE-AMOUNT",
        }]
    return payload


def _finalize_synthetic(payload: dict, tmp_path: Path):
    calls = {"normalize": 0, "readiness": 0, "provenance": 0}

    def normalize(_result):
        calls["normalize"] += 1

    def readiness(result):
        calls["readiness"] += 1
        for invoice in result.get("all_invoices") or []:
            invoice["_experiment_readiness"] = {
                "status": "blocked", "export_allowed": False,
            }

    def provenance(_result):
        calls["provenance"] += 1

    path = tmp_path / "processed" / "_webapp_result.json"
    result = _finalize_controlled_processor_result(
        payload, result_path=path, normalize_result=normalize,
        attach_readiness=readiness, assert_provenance=provenance,
    )
    return result, path, calls


def test_processing_failure_with_evidence_backed_review_persists_review_required(tmp_path: Path):
    result, path, calls = _finalize_synthetic(_controlled_failure_payload("review"), tmp_path)
    disposition = result["phase_a_terminal_disposition"]
    assert path.exists()
    assert disposition["disposition"] == PhaseATerminalDisposition.REVIEW_REQUIRED.value
    assert disposition["review_required"] is True
    assert disposition["accepted"] is False
    assert result["export_allowed"] is False
    assert result["all_invoices"] == []
    assert calls == {"normalize": 0, "readiness": 0, "provenance": 0}


def test_unresolved_evidence_backed_invoice_is_review_required_not_accepted(
    tmp_path: Path,
):
    payload = {
        "success": True,
        "summary": {
            "processing_mode": "ai_assisted",
            "files_total": 1,
            "files_processed": 1,
            "processing_failures": 0,
            "invoices_produced": 1,
            "reconciliation_ran": True,
        },
        "invoices": [{"rows": [_row()]}],
        "manual_review_rows": [{
            "reason_codes": [
                "ai_confidence_low",
                "supplementary_visual_evidence_unresolved",
                "total_reconciliation_failed",
            ],
        }],
        "unsupported_files": [],
    }
    calls = {"normalize": 0, "readiness": 0, "provenance": 0}

    result = _finalize_controlled_processor_result(
        payload,
        result_path=tmp_path / "processed" / "_webapp_result.json",
        normalize_result=lambda _value: calls.__setitem__(
            "normalize", calls["normalize"] + 1,
        ),
        attach_readiness=lambda _value: calls.__setitem__(
            "readiness", calls["readiness"] + 1,
        ),
        assert_provenance=lambda _value: calls.__setitem__(
            "provenance", calls["provenance"] + 1,
        ),
    )

    disposition = result["phase_a_terminal_disposition"]
    assert disposition["disposition"] == "review_required"
    assert disposition["document_facts_exist"] is True
    assert disposition["provenance_exists"] is True
    assert disposition["reconciliation_ran"] is True
    assert disposition["processing_failure_count"] == 0
    assert disposition["sanitized_failure_code"] == (
        "supplementary_visual_evidence_unresolved"
    )
    assert disposition["accepted"] is False
    assert result["export_allowed"] is False
    assert result["manual_review_rows"] == [{
        "review_required": True,
        "reason_codes": [
            "supplementary_visual_evidence_unresolved",
            "total_reconciliation_failed",
        ],
    }]
    assert calls == {"normalize": 0, "readiness": 0, "provenance": 0}


def test_processing_failure_with_structural_unsupported_payload_persists_unsupported(tmp_path: Path):
    result, path, _calls = _finalize_synthetic(
        _controlled_failure_payload("unsupported"), tmp_path,
    )
    assert result["phase_a_terminal_disposition"]["disposition"] == "unsupported"
    assert result["unsupported_files"] == [{
        "unsupported": True,
        "sanitized_failure_code": "ai_response_invalid_json",
    }]
    assert json.loads(path.read_text(encoding="utf-8"))["export_allowed"] is False


def test_processing_failure_without_reviewable_payload_persists_blocked(tmp_path: Path):
    result, path, calls = _finalize_synthetic(_controlled_failure_payload("blocked"), tmp_path)
    assert result["phase_a_terminal_disposition"]["disposition"] == "blocked"
    assert result["phase_a_terminal_disposition"]["sanitized_failure_code"] == "processor_failure"
    assert path.exists()
    assert calls["readiness"] == 0


def test_failed_result_sanitization_excludes_private_values_and_raw_payload(tmp_path: Path):
    result, path, _calls = _finalize_synthetic(_controlled_failure_payload("review"), tmp_path)
    serialized = path.read_text(encoding="utf-8")
    assert "PRIVATE-" not in serialized
    assert "message" not in serialized
    assert "filename" not in serialized
    assert "raw_provider_response" not in serialized
    assert result["manual_review_rows"] == [{
        "review_required": True,
        "reason_codes": ["total_reconciliation_failed"],
    }]


def test_single_and_segmented_failure_routes_use_same_terminal_contract():
    single = _controlled_failure_payload("review")
    segmented = _controlled_failure_payload("review")
    segmented["summary"]["processing_mode"] = "ai_assisted_segmented"
    assert _derive_terminal_disposition(single) == _derive_terminal_disposition(segmented)


def test_successful_valid_document_facts_behavior_remains_downstream_authorized(tmp_path: Path):
    row = _row()
    payload = {
        "success": True,
        "summary": {"processing_failures": 0, "invoices_produced": 1},
        "invoices": [{"source_file": "safe.pdf", "rows": [row]}],
        "manual_review_rows": [],
        "unsupported_files": [],
    }
    result, path, calls = _finalize_synthetic(payload, tmp_path)
    disposition = result["phase_a_terminal_disposition"]
    assert disposition["disposition"] == "accepted"
    assert disposition["document_facts_exist"] is True
    assert calls == {"normalize": 1, "readiness": 1, "provenance": 1}
    assert path.exists()


def test_synthetic_review_replay_records_disposition_and_keeps_gate_failed(tmp_path: Path):
    result, _path, _calls = _finalize_synthetic(
        _controlled_failure_payload("review"), tmp_path,
    )
    split = tmp_path / "split"
    (split / "scopes").mkdir(parents=True)
    (split / "hidden").mkdir()
    for relative in (
        "scopes/training_labels.jsonl", "scopes/benchmark_only_labels.jsonl",
        "scopes/rule_simulation_labels.jsonl", "hidden/holdout_labels.jsonl",
    ):
        (split / relative).write_text("", encoding="utf-8")
    frozen = tmp_path / "frozen.json"
    frozen.write_text(json.dumps({
        "source_map": {"safe.pdf": {"unit_id": "unit-one", "document_id": "doc-one"}},
        "shard_results": [result],
        "shard_failures": [],
    }), encoding="utf-8")
    import hashlib
    digest = hashlib.sha256(frozen.read_bytes()).hexdigest()
    metrics = _evaluate_frozen_baseline(
        frozen_path=frozen, expected_frozen_sha256=digest, split_root=split,
        controller=ExperimentSpendController(tmp_path / "private", "exp-test"),
        wall_seconds=0.1,
        controlled_audit={"passed": True, "failure_codes": []},
    )
    _finalize_controlled_gate_metrics(metrics)
    assert metrics["documents_accepted"] == 0
    assert metrics["recorded_disposition_count"] == 1
    assert metrics["review_required_document_count"] == 1
    assert metrics["false_safe_export_count"] == 0
    assert metrics["provider_calls"] == 0
    assert metrics["controlled_gate_audit"]["passed"] is False
    assert "processing_failure" in metrics["controlled_gate_audit"]["failure_codes"]
    assert "document_without_recorded_disposition" not in metrics["controlled_gate_audit"]["failure_codes"]
