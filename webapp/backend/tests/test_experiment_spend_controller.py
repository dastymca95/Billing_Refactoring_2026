from __future__ import annotations

import json

import pytest

from webapp.backend.services import ai_provider, ai_runtime_trace
from webapp.backend.services.experiment_spend_controller import (
    activate_experiment_spend_gate,
    current_experiment_spend_gate,
    ExperimentSpendController,
    SpendAuthorizationError,
)


def test_reserves_before_dispatch_and_records_provider_usage(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    reservation = controller.reserve(
        phase="A", estimated_cost_usd="0.20", provider="openai",
        model_id="configured-model", profile_id="audit-profile", stage="calibration",
    )
    assert controller.snapshot().active_reserved_usd == "0.200000"
    controller.mark_dispatched(reservation.reservation_id)
    settled = controller.settle(
        reservation.reservation_id,
        actual_cost_usd="0.12",
        usage={"input_tokens": 100, "output_tokens": 20, "secret": "excluded"},
        provider_reported_usage=True,
    )
    assert settled.actual_cost_usd == "0.120000"
    assert settled.usage == {"input_tokens": 100, "output_tokens": 20}
    snapshot = controller.snapshot()
    assert snapshot.cumulative_charged_usd == "0.120000"
    assert snapshot.active_reserved_usd == "0.000000"


def test_reserved_request_cannot_be_settled_without_dispatch(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    reservation = controller.reserve(
        phase="A", estimated_cost_usd="0.20", provider="openai",
        model_id="configured-model", profile_id="audit-profile", stage="calibration",
    )
    with pytest.raises(SpendAuthorizationError, match="marked_dispatched"):
        controller.settle(reservation.reservation_id, actual_cost_usd="0.10")
    released = controller.release_reserved(reservation.reservation_id, reason="local_validation_failed")
    assert released.status == "aborted_before_dispatch"
    assert controller.snapshot().projected_usd == "0.000000"


def test_explicit_operator_reauthorization_preserves_history_and_cap(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    reservation = controller.reserve(
        phase="A", estimated_cost_usd="0.20", provider="gemini",
        model_id="configured-model", profile_id="runtime-vision", stage="probe",
    )
    controller.mark_dispatched(reservation.reservation_id)
    controller.settle(
        reservation.reservation_id,
        actual_cost_usd=None,
        provider_reported_usage=False,
        failure_code="http_400",
    )
    controller.cancel_outstanding(reason="provider_usage_or_pricing_indeterminate")
    assert controller.snapshot().canceled is True

    controller.reauthorize_dispatch_after_operator_approval(
        phase="A",
        expected_cancel_reason="provider_usage_or_pricing_indeterminate",
        actor="operator",
        authorization_reference="synthetic_probe_authorization",
    )
    snapshot = controller.snapshot()
    assert snapshot.canceled is False
    assert snapshot.cumulative_charged_usd == "0.200000"
    followup = controller.reserve(
        phase="A", estimated_cost_usd="0.01", provider="gemini",
        model_id="configured-model", profile_id="runtime-vision", stage="probe",
    )
    controller.release_reserved(followup.reservation_id, reason="test_cleanup")
    events = controller.events_path.read_text(encoding="utf-8")
    assert "experiment_canceled" in events
    assert "experiment_dispatch_reauthorized" in events


def test_operator_reauthorization_rejects_changed_reason(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    controller.cancel_outstanding(reason="provider_usage_or_pricing_indeterminate")
    with pytest.raises(SpendAuthorizationError, match="cancel_reason_changed"):
        controller.reauthorize_dispatch_after_operator_approval(
            phase="A", expected_cancel_reason="different_reason",
            actor="operator", authorization_reference="synthetic_probe_authorization",
        )


def test_phase_caps_are_checked_before_dispatch_and_alerts_are_emitted(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    controller.reserve(
        phase="A", estimated_cost_usd="5.00", provider="openai",
        model_id="configured-model", profile_id="audit-profile", stage="calibration",
    )
    assert controller.snapshot().alerts_emitted == [50]
    with pytest.raises(SpendAuthorizationError, match="phase_cost_cap_would_be_exceeded"):
        controller.reserve(
            phase="A", estimated_cost_usd="5.01", provider="openai",
            model_id="configured-model", profile_id="audit-profile", stage="calibration",
        )


def test_phase_a_alerts_cover_50_75_90_and_100_percent(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    reservations = []
    for amount, expected in (
        ("5.00", [50]),
        ("2.50", [50, 75]),
        ("1.50", [50, 75, 90]),
        ("1.00", [50, 75, 90, 100]),
    ):
        reservations.append(controller.reserve(
            phase="A", estimated_cost_usd=amount, provider="gemini",
            model_id="configured-model", profile_id="vision", stage="calibration",
        ))
        assert controller.snapshot().alerts_emitted == expected
    for reservation in reservations:
        controller.release_reserved(reservation.reservation_id, reason="test_cleanup")


def test_phase_cap_includes_other_provider_cost_and_reports_openai_separately(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    reservation = controller.reserve(
        phase="A", estimated_cost_usd="2.00", provider="gemini",
        model_id="configured-model", profile_id="vision", stage="calibration",
    )
    assert controller.snapshot().projected_usd == "2.000000"
    assert controller.snapshot().openai_projected_usd == "0.000000"
    controller.mark_dispatched(reservation.reservation_id)
    controller.settle(reservation.reservation_id, actual_cost_usd="1.50")
    snapshot = controller.snapshot()
    assert snapshot.cumulative_charged_usd == "1.500000"
    assert snapshot.openai_cumulative_charged_usd == "0.000000"
    assert snapshot.by_provider_profile["gemini:vision:configured-model"]["charged_cost_usd"] == "1.500000"
    with pytest.raises(SpendAuthorizationError, match="phase_cost_cap_would_be_exceeded"):
        controller.reserve(
            phase="A", estimated_cost_usd="8.51", provider="deepseek",
            model_id="configured-model", profile_id="reasoning", stage="calibration",
        )


def test_future_phases_require_explicit_report_acceptance(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    with pytest.raises(SpendAuthorizationError, match="phase_b_requires"):
        controller.reserve(
            phase="B", estimated_cost_usd="1", provider="openai",
            model_id="configured-model", profile_id="pilot", stage="pilot",
        )
    controller.accept_phase_report("A", report_sha256="a" * 64, actor="operator")
    controller.reserve(
        phase="B", estimated_cost_usd="1", provider="openai",
        model_id="configured-model", profile_id="pilot", stage="pilot",
    )
    with pytest.raises(SpendAuthorizationError, match="phase_c_requires"):
        controller.reserve(
            phase="C", estimated_cost_usd="1", provider="openai",
            model_id="configured-model", profile_id="full", stage="full",
        )


def test_phase_b_report_cannot_be_accepted_before_phase_a(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    with pytest.raises(SpendAuthorizationError, match="requires_phase_a"):
        controller.accept_phase_report("B", report_sha256="b" * 64, actor="operator")


def test_phase_report_cannot_be_accepted_with_outstanding_dispatch(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    controller.reserve(
        phase="A", estimated_cost_usd="0.01", provider="openai",
        model_id="configured-model", profile_id="audit-profile", stage="calibration",
    )
    with pytest.raises(SpendAuthorizationError, match="outstanding_requests"):
        controller.accept_phase_report("A", report_sha256="a" * 64, actor="operator")


def test_cancel_outstanding_prevents_future_dispatch(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    controller.reserve(
        phase="A", estimated_cost_usd="1", provider="openai",
        model_id="configured-model", profile_id="audit-profile", stage="calibration",
    )
    assert controller.cancel_outstanding(reason="operator_stop") == 1
    assert controller.snapshot().active_reserved_usd == "0.000000"
    with pytest.raises(SpendAuthorizationError, match="experiment_dispatch_canceled"):
        controller.reserve(
            phase="A", estimated_cost_usd="1", provider="openai",
            model_id="configured-model", profile_id="audit-profile", stage="calibration",
        )


def test_cancel_requests_dispatched_work_and_retains_conservative_charge(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    reservation = controller.reserve(
        phase="A", estimated_cost_usd="1", provider="openai",
        model_id="configured-model", profile_id="audit-profile", stage="calibration",
    )
    called: list[bool] = []
    controller.mark_dispatched(
        reservation.reservation_id, cancel_callback=lambda: called.append(True) or True,
    )
    assert controller.cancel_outstanding(reason="operator_stop") == 1
    assert called == [True]
    snapshot = controller.snapshot()
    assert snapshot.active_reserved_usd == "1.000000"
    assert snapshot.outstanding_reservation_ids == [reservation.reservation_id]
    controller.settle(reservation.reservation_id, actual_cost_usd=None, failure_code="canceled")
    assert controller.snapshot().cumulative_charged_usd == "1.000000"


def test_private_ledger_contains_no_credentials_or_request_bodies(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    reservation = controller.reserve(
        phase="A", estimated_cost_usd="0.01", provider="gemini",
        model_id="configured-model", profile_id="vision", stage="facts",
    )
    controller.mark_dispatched(reservation.reservation_id)
    controller.settle(reservation.reservation_id, actual_cost_usd=None, failure_code="timeout")
    rendered = controller.path.read_text(encoding="utf-8") + controller.events_path.read_text(encoding="utf-8")
    assert "api_key" not in rendered.casefold()
    assert "authorization" not in rendered.casefold()
    assert "prompt" not in rendered.casefold()
    assert "source_file" not in rendered.casefold()
    assert json.loads(controller.path.read_text(encoding="utf-8"))["experiment_id"] == "exp-test"


def test_spend_gate_is_explicit_and_context_local(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    assert current_experiment_spend_gate() is None
    with activate_experiment_spend_gate(
        controller, phase="A", pricing_version="private-rate-card-v1",
    ) as gate:
        assert current_experiment_spend_gate() is gate
        assert gate.phase.value == "A"
    assert current_experiment_spend_gate() is None


class _ProviderResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int):
        return self.payload


def test_chat_transport_reserves_before_network_and_settles_reported_usage(
    tmp_path, monkeypatch,
):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    monkeypatch.setattr(
        ai_provider.urllib.request, "urlopen",
        lambda *_args, **_kwargs: _ProviderResponse({
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        }),
    )
    with ai_runtime_trace.operation(
        batch_id="", stage="synthetic", provider="openai",
        model="configured-model", profile_id="runtime-text",
    ):
        ai_runtime_trace.update_context(
            estimated_cost_usd=0.10,
            input_cost_usd_per_million=1.0,
            output_cost_usd_per_million=2.0,
        )
        with activate_experiment_spend_gate(
            controller, phase="A", pricing_version="private-rate-card-v1",
        ):
            assert ai_provider._send_chat_completion(
                provider="openai",
                payload={"model": "configured-model", "messages": []},
                api_key_override="not-a-real-secret",
                base_url_override="https://invalid.local/v1",
                max_attempts_override=1,
            ) == "{}"
    snapshot = controller.snapshot()
    assert snapshot.cumulative_charged_usd == "0.002000"
    assert snapshot.outstanding_reservation_ids == []


def test_native_response_transport_uses_same_spend_authority(tmp_path, monkeypatch):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    monkeypatch.setattr(
        ai_provider.urllib.request, "urlopen",
        lambda *_args, **_kwargs: _ProviderResponse({
            "output_text": "{}",
            "usage": {"input_tokens": 2000, "output_tokens": 1000, "total_tokens": 3000},
        }),
    )
    with ai_runtime_trace.operation(
        batch_id="", stage="synthetic-native", provider="openai",
        model="configured-model", profile_id="runtime-vision",
    ):
        ai_runtime_trace.update_context(
            estimated_cost_usd=0.10,
            input_cost_usd_per_million=1.0,
            output_cost_usd_per_million=2.0,
        )
        with activate_experiment_spend_gate(
            controller, phase="A", pricing_version="private-rate-card-v1",
        ):
            content, usage = ai_provider._send_openai_response(
                payload={"model": "configured-model", "input": []},
                api_key="not-a-real-secret",
                base_url="https://invalid.local/v1",
                timeout_seconds=30,
                max_attempts=1,
            )
    assert content == "{}"
    assert usage["total_tokens"] == 3000
    assert controller.snapshot().cumulative_charged_usd == "0.004000"


def test_experiment_transport_blocks_missing_cost_before_network(tmp_path, monkeypatch):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    called: list[bool] = []
    monkeypatch.setattr(
        ai_provider.urllib.request, "urlopen",
        lambda *_args, **_kwargs: called.append(True),
    )
    with ai_runtime_trace.operation(
        batch_id="", stage="synthetic", provider="openai",
        model="configured-model", profile_id="runtime-text",
    ):
        with activate_experiment_spend_gate(
            controller, phase="A", pricing_version="private-rate-card-v1",
        ):
            with pytest.raises(ai_provider.AIProviderUnavailable) as exc:
                ai_provider._send_chat_completion(
                    provider="openai",
                    payload={"model": "configured-model", "messages": []},
                    api_key_override="not-a-real-secret",
                    base_url_override="https://invalid.local/v1",
                    max_attempts_override=1,
                )
    assert exc.value.failure_code == "experiment_cost_estimate_missing"
    assert called == []


def test_experiment_mode_blocks_transport_when_worker_loses_gate(monkeypatch):
    called: list[bool] = []
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_MODE", "1")
    monkeypatch.setattr(
        ai_provider.urllib.request, "urlopen",
        lambda *_args, **_kwargs: called.append(True),
    )
    with pytest.raises(ai_provider.AIProviderUnavailable) as exc:
        ai_provider._send_chat_completion(
            provider="openai",
            payload={"model": "configured-model", "messages": []},
            api_key_override="not-a-real-secret",
            base_url_override="https://invalid.local/v1",
            max_attempts_override=1,
        )
    assert exc.value.failure_code == "experiment_spend_gate_missing"
    assert called == []


def test_bounded_workers_propagate_spend_gate_to_every_network_attempt(
    tmp_path, monkeypatch,
):
    from pathlib import Path
    from webapp.backend.services import ai_invoice_processor

    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    network_calls: list[bool] = []

    def fake_urlopen(*_args, **_kwargs):
        network_calls.append(True)
        return _ProviderResponse({
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        })

    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_MODE", "1")
    monkeypatch.setattr(ai_provider.urllib.request, "urlopen", fake_urlopen)

    def worker(source_file: Path):
        assert current_experiment_spend_gate() is not None
        assert ai_provider._send_chat_completion(
            provider="openai",
            payload={"model": "configured-model", "messages": []},
            api_key_override="not-a-real-secret",
            base_url_override="https://invalid.local/v1",
            max_attempts_override=1,
        ) == "{}"
        return {"source": source_file.name}

    with ai_runtime_trace.operation(
        batch_id="", stage="synthetic-workers", provider="openai",
        model="configured-model", profile_id="runtime-text",
    ):
        ai_runtime_trace.update_context(
            estimated_cost_usd=0.01,
            input_cost_usd_per_million=1.0,
            output_cost_usd_per_million=2.0,
        )
        with activate_experiment_spend_gate(
            controller, phase="A", pricing_version="private-rate-card-v1",
        ):
            results = ai_invoice_processor._run_ai_file_workers_bounded(
                [Path(f"safe-{index}.pdf") for index in range(4)],
                worker,
                max_workers=4,
            )

    assert len(results) == 4
    assert all(error is None for _path, _value, error in results)
    assert len(network_calls) == 4
    assert controller.snapshot().outstanding_reservation_ids == []
    state = json.loads(controller.path.read_text(encoding="utf-8"))
    assert len(state["reservations"]) == 4
    assert {row["status"] for row in state["reservations"].values()} == {"settled"}


def test_spend_telemetry_rejects_private_path_or_document_filename(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-test")
    with pytest.raises(ValueError, match="private_path"):
        controller.reserve(
            phase="A", estimated_cost_usd="0.01", provider="openai",
            model_id="configured-model", profile_id="runtime-text",
            stage="C:\\private\\client-invoice.pdf",
        )
