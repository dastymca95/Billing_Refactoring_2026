from __future__ import annotations

import inspect
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from webapp.backend.services import supplementary_ab_experiment as ab
from webapp.backend.services.experiment_spend_controller import ExperimentSpendController


def _material(index: int):
    content = f"synthetic-crop-{index}".encode()
    metadata = {
        "crop_id": f"crop-{index}", "role": "primary_target", "category": "totals_footer",
        "ordinal": 0, "mime_type": "image/jpeg", "width": 10, "height": 8,
        "pixel_count": 80, "sha256": ab.sha256_bytes(content), "byte_length": len(content),
    }
    prompt = b"Synthetic supplementary visual question"
    schema = ab.canonical_json_bytes({"type": "object", "properties": {"visible": {"type": "boolean"}}})
    packet = ab.frame_packet_bytes(((metadata, content),))
    return metadata, content, prompt, schema, packet


def _manifest(tmp_path: Path):
    root = tmp_path / "private-bundle"; root.mkdir()
    records = []
    for index in range(5):
        metadata, crop_bytes, prompt, schema, packet = _material(index)
        directory = root / f"packet_{index:02d}"; directory.mkdir()
        (directory / "crop_00.jpg").write_bytes(crop_bytes)
        (directory / "prompt.utf8").write_bytes(prompt)
        (directory / "schema.json").write_bytes(schema)
        (directory / "packet.bin").write_bytes(packet)
        crop = ab.FrozenCropReference(
            **metadata, relative_blob_path=f"packet_{index:02d}/crop_00.jpg",
        )
        records.append(ab.FrozenPacketRecord(
            packet_id=f"packet-{index}", plan_id=f"plan-{index}",
            target_category="total_mismatch", target_subtype="missing_tax_or_fee",
            opaque_source_sha256=ab.sha256_bytes(f"source-{index}".encode()),
            relative_packet_path=f"packet_{index:02d}/packet.bin",
            packet_sha256=ab.sha256_bytes(packet), packet_byte_length=len(packet),
            total_pixels=80, crops=(crop,),
            relative_prompt_path=f"packet_{index:02d}/prompt.utf8",
            prompt_sha256=ab.sha256_bytes(prompt), prompt_byte_length=len(prompt),
            relative_schema_path=f"packet_{index:02d}/schema.json",
            schema_sha256=ab.sha256_bytes(schema), schema_byte_length=len(schema),
            generation_settings_fingerprint=ab.sha256_bytes(b"settings"),
            planner_fingerprint=ab.sha256_bytes(b"planner"), offline_regeneration_equal=True,
        ))
    refs = tuple(ref for record in records for ref in ab.build_paired_references(record))
    excluded = tuple(ab.ExcludedLocalizationTarget(
        target_id=f"excluded-{index}", target_category="invoice_number_ambiguity",
    ) for index in range(2))
    probe = ab.SyntheticCapabilityProbeContract(
        image_sha256=ab.sha256_bytes(b"synthetic-image"),
        prompt_sha256=ab.sha256_bytes(b"synthetic-prompt"),
        schema_sha256=ab.sha256_bytes(b"synthetic-schema"),
    )
    return ab.FrozenPairedManifest(
        source_run_id="historical-run-reference-only",
        source_manifest_sha256=ab.sha256_bytes(b"source-manifest"),
        packet_records=tuple(records), arm_references=refs,
        excluded_localization_targets=excluded, capability_probe=probe,
    ), root


def test_fresh_pair_has_two_future_models_and_no_historical_formal_arm(tmp_path):
    manifest, _ = _manifest(tmp_path)
    assert manifest.historical_results_are_formal_arm is False
    assert {item.model_id for item in manifest.arm_references} == {
        "gemini-3.1-flash-lite", "gemini-3.5-flash",
    }


def test_exactly_five_eligible_and_two_excluded_targets(tmp_path):
    manifest, _ = _manifest(tmp_path)
    assert len(manifest.packet_records) == 5
    assert len(manifest.excluded_localization_targets) == 2
    assert all(item.provider_calls == 0 for item in manifest.excluded_localization_targets)


def test_every_packet_has_byte_identical_arm_references(tmp_path):
    manifest, _ = _manifest(tmp_path)
    for record in manifest.packet_records:
        refs = [item for item in manifest.arm_references if item.packet_id == record.packet_id]
        assert len(refs) == 2
        assert len({(r.packet_sha256, r.prompt_sha256, r.schema_sha256, r.ordered_crop_sha256s) for r in refs}) == 1


def test_pair_validation_rejects_one_changed_arm_hash(tmp_path):
    manifest, _ = _manifest(tmp_path)
    changed = manifest.arm_references[0].model_copy(update={"prompt_sha256": "0" * 64})
    with pytest.raises(ValidationError, match="identical_bytes"):
        ab.FrozenPairedManifest(**{
            **manifest.model_dump(), "arm_references": (changed, *manifest.arm_references[1:]),
        })


def test_material_loader_verifies_packet_prompt_schema_and_crop_bytes(tmp_path):
    manifest, root = _manifest(tmp_path)
    record = manifest.packet_records[0]
    packet, prompt, schema, crops = ab.load_verified_packet_material(root, record)
    assert ab.sha256_bytes(packet) == record.packet_sha256
    assert prompt and schema and crops
    (root / record.crops[0].relative_blob_path).write_bytes(b"mutated")
    with pytest.raises(ab.ABContractError, match="integrity"):
        ab.load_verified_packet_material(root, record)


def test_generation_settings_and_planner_fingerprints_are_mandatory(tmp_path):
    manifest, _ = _manifest(tmp_path)
    record = manifest.packet_records[0]
    with pytest.raises(ValidationError):
        ab.FrozenPacketRecord(**{**record.model_dump(), "planner_fingerprint": "missing"})


def test_offline_regeneration_equality_is_recorded_per_packet(tmp_path):
    manifest, _ = _manifest(tmp_path)
    assert [item.offline_regeneration_equal for item in manifest.packet_records] == [True] * 5


def test_synthetic_probe_is_not_executed_and_requires_operator_confirmation(tmp_path):
    manifest, _ = _manifest(tmp_path)
    assert manifest.capability_probe.synthetic_only is True
    assert manifest.capability_probe.execute_during_preparation is False
    assert manifest.capability_probe.maximum_requests == 1
    assert manifest.capability_probe.project_availability == "operator_confirmation_required"
    assert manifest.capability_probe.verify_project_authentication is True
    assert manifest.capability_probe.verify_model_availability is True
    assert manifest.capability_probe.verify_image_input is True
    assert manifest.capability_probe.verify_structured_output is True
    assert manifest.capability_probe.usage_and_cost_reporting_required is True
    assert manifest.capability_probe.response_persistence_allowed is False


def test_execution_policy_is_one_shot_gemini_only_and_has_no_repair_routes():
    policy = ab.PairedExecutionPolicy()
    assert policy.maximum_total_requests == 10
    assert policy.maximum_requests_per_packet_per_arm == 1
    assert policy.retries == 0
    assert not any((policy.fallback_enabled, policy.extraction_enabled, policy.crop_regeneration_enabled,
                    policy.reconstruction_enabled, policy.second_supplement_enabled,
                    policy.repair_enabled, policy.other_provider_enabled))
    assert policy.temperature == 0
    assert policy.max_output_tokens == 2048


def test_one_request_per_packet_per_model_and_spend_before_dispatch(tmp_path):
    manifest, _ = _manifest(tmp_path)
    budget = ab.calculate_paired_budget(
        expected_input_tokens_per_arm=27_507, expected_output_tokens_per_arm=3_719,
        phase_a_cumulative_spend_usd=Decimal("0.280834"),
    )
    controller = ExperimentSpendController(tmp_path / "spend", "exp-paired-offline")
    ledger = ab.OneShotPairedLedger(manifest)
    with pytest.raises(ab.ABContractError, match="without_reservation"):
        ledger.consume("packet-0", ab.ExperimentArm.A)
    reservation = ab.reserve_packet_arm(
        controller=controller, packet_id="packet-0", arm=ab.ExperimentArm.A, budget=budget,
    )
    ledger.register("packet-0", ab.ExperimentArm.A, reservation)
    ledger.consume("packet-0", ab.ExperimentArm.A)
    with pytest.raises(ab.ABContractError, match="request_limit"):
        ledger.consume("packet-0", ab.ExperimentArm.A)
    controller.release_reserved(reservation.reservation_id, reason="offline_test_cleanup")


def test_arm_outputs_are_isolated_and_cannot_be_context_for_other_arm(tmp_path):
    manifest, _ = _manifest(tmp_path)
    budget = ab.calculate_paired_budget(
        expected_input_tokens_per_arm=100, expected_output_tokens_per_arm=20,
        phase_a_cumulative_spend_usd=Decimal("0"),
    )
    controller = ExperimentSpendController(tmp_path / "spend", "exp-output-isolation")
    ledger = ab.OneShotPairedLedger(manifest)
    reservation = ab.reserve_packet_arm(controller=controller, packet_id="packet-0", arm=ab.ExperimentArm.A, budget=budget)
    ledger.register("packet-0", ab.ExperimentArm.A, reservation); ledger.consume("packet-0", ab.ExperimentArm.A)
    ledger.record_output("packet-0", ab.ExperimentArm.A, "a" * 64)
    assert ledger.isolated_output("packet-0", ab.ExperimentArm.A) == "a" * 64
    assert ledger.isolated_output("packet-0", ab.ExperimentArm.B) is None
    controller.release_reserved(reservation.reservation_id, reason="offline_test_cleanup")


def test_budget_is_realistic_under_one_dollar_and_global_cap():
    budget = ab.calculate_paired_budget(
        expected_input_tokens_per_arm=27_507, expected_output_tokens_per_arm=3_719,
        phase_a_cumulative_spend_usd=Decimal("0.280834"),
    )
    assert budget.expected_total_usd == Decimal("0.087188")
    assert budget.maximum_reserved_total_usd < Decimal("1")
    assert budget.phase_a_remaining_after_maximum_usd > 0
    assert {item.maximum_output_tokens for item in budget.model_budgets} == {10_240}


def test_budget_fails_closed_when_ab_subcap_is_exceeded():
    with pytest.raises(ValidationError, match="sub_budget"):
        ab.calculate_paired_budget(
            expected_input_tokens_per_arm=1_000_000,
            expected_output_tokens_per_arm=10_000,
            phase_a_cumulative_spend_usd=Decimal("0"),
        )


@pytest.mark.parametrize("outcome", [ab.PacketOutcome.UNRESOLVED, ab.PacketOutcome.CONTRADICTION, ab.PacketOutcome.INVALID])
def test_unresolved_contradictory_or_invalid_cannot_be_safe(outcome):
    with pytest.raises(ValidationError, match="unsafe_paired_outcome"):
        ab.PacketArmEvaluation(
            packet_id="packet", arm=ab.ExperimentArm.A, schema_valid=outcome is not ab.PacketOutcome.INVALID,
            outcome=outcome, visible_candidate_count=1, resolved_candidate_count=0,
            contradiction=outcome is ab.PacketOutcome.CONTRADICTION,
            unresolved=outcome is ab.PacketOutcome.UNRESOLVED,
            reconciliation_before="unreconciled", reconciliation_after="unreconciled",
            strict_document_facts_recovered=False,
            disposition="accepted", accepted=True, export_allowed=True, false_safe_export=True,
            latency_ms=1, input_tokens=1, output_tokens=1, actual_cost_usd=Decimal("0.001"),
        )


def test_excluded_target_cannot_be_added_to_pair(tmp_path):
    manifest, _ = _manifest(tmp_path)
    changed = manifest.packet_records[0].model_copy(update={"packet_id": "excluded-0"})
    with pytest.raises(ValidationError, match="excluded"):
        ab.FrozenPairedManifest(**{
            **manifest.model_dump(), "packet_records": (changed, *manifest.packet_records[1:]),
        })


def test_module_has_no_provider_extraction_accounting_or_readiness_authority():
    source = inspect.getsource(ab).casefold()
    for forbidden in ("httpx", "requests.", "urllib", "ai_invoice_processor",
                      "accounting_decision_engine", "accounting_readiness", "send_chat_completion"):
        assert forbidden not in source


def test_git_safe_summary_contains_aggregates_only(tmp_path):
    manifest, _ = _manifest(tmp_path)
    budget = ab.calculate_paired_budget(
        expected_input_tokens_per_arm=27_507, expected_output_tokens_per_arm=3_719,
        phase_a_cumulative_spend_usd=Decimal("0.280834"),
    )
    summary = ab.git_safe_summary(manifest, budget)
    serialized = json.dumps(summary, sort_keys=True).casefold()
    for forbidden in ("packet_sha256", "crop_sha256", "prompt_sha256", "schema_sha256",
                      "relative_blob_path", "source_run_id", "filename", "account_number", "address"):
        assert forbidden not in serialized
    assert summary["external_provider_calls"] == 0
    assert summary["historical_results_are_formal_arm"] is False
