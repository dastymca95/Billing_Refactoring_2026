"""One-request private supplementary contract smoke test.

This runner is intentionally not an A/B runner. It selects one frozen packet
whose historical Flash-Lite response failed the old broad schema boundary,
dispatches exactly once, and persists only private-value-free structural
diagnostics under the ignored experiment root.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.review_phase_a_supplementary_evidence import (  # noqa: E402
    _batch_id_for_source,
    _experiment_root,
    _find_saved_observed_facts,
)
from scripts.run_phase_a_paired_supplementary_ab import (  # noqa: E402
    AUTHORIZED_HOST,
    BUNDLE_NAME,
    FROZEN_SETTINGS,
    PairedExecutionFailure,
    _actual_cost,
    _atomic_json,
    _native_text,
    _preflight,
    _reconciliation_name,
    _request_payload,
    _safe_provider_error,
    _target_for_record,
    _tree_sha256,
    _usage,
)
from webapp.backend.services.experiment_spend_controller import (  # noqa: E402
    ExperimentSpendController,
    spend_cost_accounting_view,
)
from webapp.backend.services.gemini_supplementary_transport import (  # noqa: E402
    SUPPLEMENTARY_TRANSPORT_V2_VERSION,
    parse_supplementary_transport_v2_response_with_audit,
    supplementary_transport_v2_family_sha256,
    supplementary_transport_v2_packet_schema_sha256,
)
from webapp.backend.services.gemini_supplementary_verification import (  # noqa: E402
    SupplementaryFailureStage,
    SupplementarySafeDiagnostics,
    SupplementaryStageStatus,
    SupplementaryVerificationError,
    merge_supplementary_observations,
    reconciliation_snapshot,
    validate_observation_crop_references,
)
from webapp.backend.services.supplementary_ab_arm_b_candidate import (  # noqa: E402
    ARM_A_MODEL_ID,
)
from webapp.backend.services.supplementary_ab_experiment import (  # noqa: E402
    AB_SERIALIZATION_VERSION,
    ExperimentArm,
    FrozenPacketRecord,
    canonical_json_sha256,
    load_verified_packet_material,
    sha256_bytes,
)
from webapp.backend.services.supplementary_crop_framing import (  # noqa: E402
    SUPPLEMENTARY_CROP_FRAMING_VERSION,
)


CONTRACT_VERSION = "phase-a-private-supplementary-contract-smoke/2.0"
AUTHORIZED_PACKET_SHA256 = (
    "385b8e3ef8f7bac593f07325d3df3a9e62a4629f5e4c7178ac39dd2e1e490b88"
)
HISTORICAL_AUTHORIZED_SCHEMA_FAMILY_SHA256 = (
    "6bba9a5e73c7ebf6d6b6cba65620b02154d6aad408e4dc090926a6fcd5bc98cd"
)
# Retained as a compatibility alias for the completed, immutable smoke.
AUTHORIZED_SCHEMA_FAMILY_SHA256 = HISTORICAL_AUTHORIZED_SCHEMA_FAMILY_SHA256
MAXIMUM_REQUESTS = 1
MAXIMUM_RESERVATION_USD = Decimal("0.005823")
EXPECTED_GENERATION_FINGERPRINT = canonical_json_sha256(FROZEN_SETTINGS)

TECHNICAL_SMOKE_DISPOSITIONS = frozenset({
    "accepted", "review_required", "unsupported", "blocked",
})
EVIDENCE_VALIDATION_FAILURE_CODES = frozenset({
    "supplementary_ambiguous_value_without_evidence",
    "supplementary_crop_reference_required",
    "supplementary_crop_role_mismatch",
    "supplementary_evidence_reference_invalid",
    "supplementary_evidence_reference_missing",
    "supplementary_evidence_required",
    "supplementary_unplanned_crop_reference",
    "supplementary_visible_value_without_evidence",
})


class ContractSmokeError(RuntimeError):
    pass


def _technical_smoke_acceptance(
    contract_result: Mapping[str, Any],
    *,
    terminal_disposition_persisted: bool,
    terminal_disposition_count: int,
) -> dict[str, Any]:
    """Evaluate the one-shot smoke without weakening accounting safety.

    A structurally valid observation may still be unresolved or contradictory.
    In that case the transport smoke passes only when the invoice remains
    non-accepted and non-exportable.  This gate is intentionally independent
    from both AccountingDecisionEngine and AccountingReadiness.
    """

    outcome = str(contract_result.get("canonical_outcome") or "")
    disposition = str(contract_result.get("final_disposition") or "")
    unresolved_or_contradictory = bool(
        contract_result.get("unresolved") or contract_result.get("contradiction")
    )
    checks = {
        "transport_validation_passed": (
            contract_result.get("transport_validation_status") == "passed"
        ),
        "transport_normalization_passed": (
            contract_result.get("transport_normalization_status") == "passed"
        ),
        "evidence_validation_passed": (
            contract_result.get("evidence_validation_status") == "passed"
        ),
        "internal_observation_constructed": (
            contract_result.get("internal_observation_status") == "constructed"
        ),
        "visible_or_ambiguous_evidence_authorized": (
            contract_result.get("visible_or_ambiguous_evidence_status") == "passed"
        ),
        "local_crop_enrichment_completed": (
            contract_result.get("crop_enrichment_status") == "passed"
        ),
        "generic_schema_reason_absent": outcome != "supplementary_invalid_schema",
        "one_terminal_disposition_persisted": bool(
            terminal_disposition_persisted
            and terminal_disposition_count == 1
            and disposition in TECHNICAL_SMOKE_DISPOSITIONS
        ),
        "unresolved_or_contradictory_remains_safe": bool(
            not unresolved_or_contradictory
            or (
                contract_result.get("accepted") is False
                and contract_result.get("export_allowed") is False
            )
        ),
        "false_safe_exports_zero": (
            contract_result.get("false_safe_export") is False
        ),
    }
    evidence_failed = (
        contract_result.get("evidence_validation_status") == "failed"
        or outcome in EVIDENCE_VALIDATION_FAILURE_CODES
    )
    technical_success = all(checks.values())
    if technical_success:
        terminal_action = "stop_technical_success"
    elif evidence_failed:
        terminal_action = "stop_evidence_validation_failed"
    else:
        terminal_action = "stop_technical_failure"
    return {
        "contract_version": "supplementary-smoke-acceptance/1.0",
        "technical_success": technical_success,
        "checks": checks,
        "terminal_action": terminal_action,
        "canonical_reason": outcome or None,
        "retry_allowed": False,
        "retry_count": 0,
        "micro_ab_started": False,
        "micro_ab_eligible_for_separate_authorization": technical_success,
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise ContractSmokeError("provider_redirect_forbidden")


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("smoke-%Y%m%dT%H%M%S%fZ")


def _load_historical_state(experiment_root: Path) -> Mapping[str, Any]:
    executions = experiment_root / "phase_a" / "supplementary_ab" / "executions"
    candidates: list[tuple[float, Mapping[str, Any]]] = []
    for path in executions.glob("*/execution_state.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if value.get("contract_version") != "phase-a-private-paired-supplementary-ab/1.0":
            continue
        candidates.append((path.stat().st_mtime, value))
    if not candidates:
        raise ContractSmokeError("historical_paired_execution_unavailable")
    return max(candidates, key=lambda item: item[0])[1]


def select_historical_invalid_flash_lite_record(
    manifest: Any, historical_state: Mapping[str, Any],
) -> FrozenPacketRecord:
    successful_requests = {
        (str(row.get("packet_id") or ""), str(row.get("arm") or ""))
        for row in historical_state.get("requests") or []
        if row.get("arm") == ExperimentArm.A.value
        and row.get("http_status") == 200
        and row.get("usage_reported") is True
        and row.get("raw_response_persisted") is False
    }
    invalid = {
        (str(row.get("packet_id") or ""), str(row.get("arm") or ""))
        for row in historical_state.get("evaluations") or []
        if row.get("arm") == ExperimentArm.A.value
        and row.get("failure_code") == "supplementary_invalid_schema"
        and row.get("schema_valid") is False
        and row.get("accepted") is False
        and row.get("export_allowed") is False
    }
    eligible = successful_requests & invalid
    for record in manifest.packet_records:
        if (record.packet_id, ExperimentArm.A.value) in eligible:
            return record
    raise ContractSmokeError("historical_invalid_flash_lite_packet_unavailable")


def _preflight_smoke(experiment_root: Path) -> dict[str, Any]:
    paired = _preflight(experiment_root)
    historical = _load_historical_state(experiment_root)
    record = select_historical_invalid_flash_lite_record(
        paired["manifest"], historical,
    )
    packet, prompt, schema, crops = load_verified_packet_material(
        paired["bundle_root"], record,
    )
    checks = {
        "bundle_sha256_verified": (
            paired["bundle_tree_sha256"]
            == historical.get("bundle_tree_sha256_before")
            == historical.get("bundle_tree_sha256_after")
        ),
        "packet_sha256_verified": sha256_bytes(packet) == record.packet_sha256,
        "ordered_crop_hashes_verified": tuple(
            sha256_bytes(item) for item in crops
        ) == tuple(item.sha256 for item in record.crops),
        "prompt_sha256_verified": sha256_bytes(prompt) == record.prompt_sha256,
        "schema_sha256_verified": sha256_bytes(schema) == record.schema_sha256,
        "planner_fingerprint_verified": (
            len(record.planner_fingerprint) == 64
            and record.offline_regeneration_equal
        ),
        "serialization_version_verified": (
            record.serialization_version == AB_SERIALIZATION_VERSION
        ),
        "generation_settings_verified": (
            record.generation_settings_fingerprint
            == EXPECTED_GENERATION_FINGERPRINT
        ),
    }
    if not all(checks.values()):
        raise ContractSmokeError("frozen_smoke_preflight_failed")
    endpoint, payload, semantic_fingerprint = _request_payload(
        arm=ExperimentArm.A,
        prompt=prompt,
        schema=schema,
        record=record,
        crops=crops,
    )
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or parsed.hostname != AUTHORIZED_HOST:
        raise ContractSmokeError("unauthorized_provider_host")
    if semantic_fingerprint != paired["semantic_fingerprints"][record.packet_id]:
        raise ContractSmokeError("frozen_semantic_fingerprint_changed")
    generation = payload["generationConfig"]
    response_schema = generation["responseJsonSchema"]
    generation_without_schema = {
        key: value for key, value in generation.items()
        if key != "responseJsonSchema"
    }
    crop_hashes = tuple(sha256_bytes(item) for item in crops)
    schema_family_fingerprint = supplementary_transport_v2_family_sha256()
    planned_crops = {
        item.crop_id: {
            "role": item.role,
            "ordinal": item.ordinal,
            "target_relevance": f"{_target_for_record(record).target_type.value}:{item.category}",
            "mime_type": item.mime_type,
            "page_number": _target_for_record(record).page_number,
            "plan_id": record.plan_id,
            "packet_sha256": record.packet_sha256,
            "source_kind": "frozen_supplementary_crop",
        }
        for item in record.crops
    }
    packet_schema_fingerprint = supplementary_transport_v2_packet_schema_sha256(
        _target_for_record(record),
        planned_crops=planned_crops,
        packet_sha256=record.packet_sha256,
    )
    direct_schema_verified = (
        "payload_json" not in response_schema.get("properties", {})
        and response_schema.get("properties", {})
        .get("contract_version", {})
        .get("enum") == [SUPPLEMENTARY_TRANSPORT_V2_VERSION]
    )
    authorization_checks = {
        "authorized_packet_verified": record.packet_sha256 == AUTHORIZED_PACKET_SHA256,
        "historical_schema_authorization_preserved": (
            AUTHORIZED_SCHEMA_FAMILY_SHA256
            == HISTORICAL_AUTHORIZED_SCHEMA_FAMILY_SHA256
        ),
        "new_schema_requires_separate_authorization": (
            schema_family_fingerprint != AUTHORIZED_SCHEMA_FAMILY_SHA256
        ),
        "direct_v2_schema_verified": direct_schema_verified,
    }
    if not all(authorization_checks.values()):
        raise ContractSmokeError("v2_smoke_authorization_preflight_failed")
    checks.update(authorization_checks)
    return {
        **paired,
        "record": record,
        "packet": packet,
        "prompt": prompt,
        "schema": schema,
        "crops": crops,
        "endpoint": endpoint,
        "payload": payload,
        "semantic_fingerprint": semantic_fingerprint,
        "v2_response_schema_fingerprint": canonical_json_sha256(response_schema),
        "complete_request_fingerprint": canonical_json_sha256(payload),
        "packet_fingerprint": record.packet_sha256,
        "ordered_crops_fingerprint": canonical_json_sha256(crop_hashes),
        "prompt_fingerprint": record.prompt_sha256,
        "frozen_semantic_schema_fingerprint": record.schema_sha256,
        "schema_family_fingerprint": schema_family_fingerprint,
        "packet_specific_schema_fingerprint": packet_schema_fingerprint,
        "ordered_crop_label_image_framing_fingerprint": canonical_json_sha256(
            payload["contents"][0]["parts"][2:]
        ),
        "generation_settings_v2_fingerprint": canonical_json_sha256(
            generation_without_schema,
        ),
        "planner_fingerprint": record.planner_fingerprint,
        "serialization_version": record.serialization_version,
        "crop_framing_version": SUPPLEMENTARY_CROP_FRAMING_VERSION,
        "checks": checks,
    }


def _assert_one_shot_available(smoke_root: Path) -> None:
    for state_path in smoke_root.glob("*/smoke_state.json"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if state.get("authorization_consumed") is True:
            raise ContractSmokeError("contract_smoke_authorization_already_consumed")


def _safe_initial_facts(preflight: Mapping[str, Any]) -> Mapping[str, Any]:
    record = preflight["record"]
    source_run: Path = preflight["source_run"]
    position = next(
        index for index, candidate in enumerate(
            preflight["manifest"].packet_records, 1,
        ) if candidate.packet_id == record.packet_id
    )
    batch_id = _batch_id_for_source(
        source_run, record.opaque_source_sha256, position=position,
    )
    initial = _find_saved_observed_facts(
        source_run, record.opaque_source_sha256, batch_id=batch_id,
    )
    return copy.deepcopy(initial) if initial is not None else {
        "line_items": [],
        "evidence": [],
        "page_reconciliations": [],
        "warnings": [],
        "needs_manual_review": True,
    }


def _observation_references(observation: Any) -> list[Any]:
    references: list[Any] = []
    if observation.evidence_reference is not None:
        references.append(observation.evidence_reference)
    if observation.observed_candidate_value is not None:
        references.extend(observation.observed_candidate_value.evidence_references)
    references.extend(observation.raw_visible_text_evidence_references)
    for candidate in observation.observed_candidates:
        references.extend(candidate.evidence_references)
    for label in observation.visible_labels:
        references.extend(label.evidence_references)
    for contradiction in observation.contradiction_observations:
        references.extend(contradiction.evidence_references)
    if observation.financial_components is not None:
        references.extend(observation.financial_components.evidence_references)
        for values in (
            observation.financial_components.component_evidence_references.values()
        ):
            references.extend(values)
    return references


def _local_crop_enrichment_complete(
    observation: Any,
    *,
    planned_crops: Mapping[str, Mapping[str, Any]],
    plan_id: str | None,
    packet_sha256: str,
) -> bool:
    """Verify locally enriched provenance without exposing crop identifiers."""

    for reference in _observation_references(observation):
        crop_id = str(reference.crop_id or "")
        planned = planned_crops.get(crop_id)
        if planned is None:
            return False
        if reference.crop_role != planned.get("role"):
            return False
        if reference.page_number != planned.get("page_number"):
            return False
        if reference.plan_id != plan_id:
            return False
        if reference.packet_sha256 != packet_sha256:
            return False
        if reference.source_kind != planned.get("source_kind"):
            return False
        if not reference.evidence_kind:
            return False
    return True


def _safe_contract_result(
    *, preflight: Mapping[str, Any], raw_text: str | None,
    initial_facts: Mapping[str, Any], finish_reason: str | None,
) -> dict[str, Any]:
    record = preflight["record"]
    target = _target_for_record(record)
    observation = None
    diagnostics: SupplementarySafeDiagnostics | None = None
    failure_code: str | None = None
    merge_executed = False
    reconciliation_state = "not_run"
    reconciliation_source_stage = "not_run"
    visible_or_ambiguous_evidence_status = "not_run"
    crop_enrichment_status = "not_run"
    if not raw_text:
        failure_code = (
            "supplementary_output_truncated"
            if finish_reason == "MAX_TOKENS"
            else "supplementary_required_field_missing"
        )
    else:
        try:
            planned_crops = {
                item.crop_id: {
                    "role": item.role,
                    "ordinal": item.ordinal,
                    "page_number": target.page_number,
                    "plan_id": getattr(record, "plan_id", None),
                    "packet_sha256": record.packet_sha256,
                    "source_kind": "frozen_supplementary_crop",
                    "target_relevance": (
                        f"{target.target_type.value}:{item.category}"
                    ),
                }
                for item in record.crops
            }
            parsed = parse_supplementary_transport_v2_response_with_audit(
                raw_text,
                target=target,
                planned_crops=planned_crops,
                plan_id=getattr(record, "plan_id", None),
                packet_sha256=record.packet_sha256,
            )
            observation = parsed.observation
            diagnostics = parsed.diagnostics
            validate_observation_crop_references(
                observation,
                allowed_crop_ids={item.crop_id for item in record.crops},
                planned_crops=planned_crops,
                expected_packet_sha256=record.packet_sha256,
                actual_packet_sha256=sha256_bytes(preflight["packet"]),
            )
            visible_or_ambiguous_evidence_status = "passed"
            if not _local_crop_enrichment_complete(
                observation,
                planned_crops=planned_crops,
                plan_id=getattr(record, "plan_id", None),
                packet_sha256=record.packet_sha256,
            ):
                raise SupplementaryVerificationError(
                    "supplementary_evidence_reference_invalid",
                    diagnostics=diagnostics.model_copy(update={
                        "stage": SupplementaryFailureStage.EVIDENCE_REFERENCE,
                        "failure_code": "supplementary_evidence_reference_invalid",
                        "evidence_reference_validation": "local_enrichment_invalid",
                        "evidence_validation_status": SupplementaryStageStatus.FAILED,
                    }),
                )
            crop_enrichment_status = "passed"
            diagnostics = diagnostics.model_copy(update={
                "crop_reference_validation": "valid",
                "evidence_reference_validation": "valid",
            })
            effective = merge_supplementary_observations(
                initial_facts, [(target, observation)],
            )
            merge_executed = True
            reconciliation_state = _reconciliation_name(
                reconciliation_snapshot(effective),
            )
            reconciliation_source_stage = "supplementary_merge"
        except SupplementaryVerificationError as exc:
            failure_code = exc.failure_code
            diagnostics = exc.diagnostics or diagnostics
            if failure_code in EVIDENCE_VALIDATION_FAILURE_CODES:
                visible_or_ambiguous_evidence_status = "failed"
                crop_enrichment_status = "failed"
            if diagnostics is not None and failure_code in {
                "supplementary_unplanned_crop_reference",
                "supplementary_crop_reference_required",
                "supplementary_crop_role_mismatch",
            }:
                diagnostics = diagnostics.model_copy(update={
                    "stage": SupplementaryFailureStage.CROP_REFERENCE,
                    "failure_code": failure_code,
                    "crop_reference_validation": "invalid",
                })
        except Exception:
            failure_code = "supplementary_internal_contract_invalid"
            if diagnostics is not None:
                diagnostics = diagnostics.model_copy(update={
                    "stage": SupplementaryFailureStage.INTERNAL_CONTRACT,
                    "failure_code": failure_code,
                })

    contract_valid = observation is not None and failure_code is None
    contradiction = bool(observation and observation.contradiction_flag)
    unresolved = bool(not contract_valid or (observation and observation.unresolved_flag))
    if not contract_valid:
        canonical_outcome = failure_code or "supplementary_internal_contract_invalid"
        disposition = "blocked"
    elif contradiction:
        canonical_outcome = "contract_valid_contradiction"
        disposition = "review_required"
    elif unresolved:
        canonical_outcome = "contract_valid_unresolved"
        disposition = "review_required"
    else:
        canonical_outcome = "contract_valid_resolved_validation_only"
        disposition = "review_required"
    return {
        "direct_transport_result": (
            "valid" if diagnostics and diagnostics.payload_present else "invalid"
        ),
        "decoder_result": (
            diagnostics.payload_parse_result if diagnostics else "not_available"
        ),
        "decode_count": diagnostics.decoding_count if diagnostics else 0,
        "normalization_result": (
            diagnostics.transport_normalization_status.value
            if diagnostics else "not_run"
        ),
        "transport_validation_status": (
            diagnostics.transport_validation_status.value
            if diagnostics else "failed"
        ),
        "transport_normalization_status": (
            diagnostics.transport_normalization_status.value
            if diagnostics else "not_run"
        ),
        "evidence_validation_status": (
            diagnostics.evidence_validation_status.value
            if diagnostics else "not_run"
        ),
        "internal_observation_status": (
            diagnostics.internal_observation_status.value
            if diagnostics else "not_constructed"
        ),
        "internal_contract_result": "valid" if contract_valid else "invalid",
        "crop_reference_result": (
            diagnostics.crop_reference_validation if diagnostics else "not_run"
        ),
        "evidence_reference_result": (
            diagnostics.evidence_reference_validation if diagnostics else "not_run"
        ),
        "visible_or_ambiguous_evidence_status": (
            visible_or_ambiguous_evidence_status
        ),
        "crop_enrichment_status": crop_enrichment_status,
        "safe_schema_diagnostics": (
            diagnostics.model_dump(mode="json") if diagnostics else None
        ),
        "canonical_outcome": canonical_outcome,
        "merge_executed": merge_executed,
        "merge_status": "passed" if merge_executed else "not_run",
        "reconciliation_status": reconciliation_state,
        "reconciliation_state": reconciliation_state,
        "reconciliation_source_stage": reconciliation_source_stage,
        "contradiction": contradiction,
        "unresolved": unresolved,
        "final_disposition": disposition,
        "accepted": False,
        "export_allowed": False,
        "false_safe_export": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-root", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    experiment_root = _experiment_root(args.experiment_root)
    preflight = _preflight_smoke(experiment_root)
    record = preflight["record"]
    preflight_summary = {
        "status": "offline_preflight_valid",
        "contract_version": CONTRACT_VERSION,
        "selected_packet_fingerprint": record.packet_sha256,
        "provider": "gemini",
        "model_id": ARM_A_MODEL_ID,
        "authorized_host": AUTHORIZED_HOST,
        "checks": preflight["checks"],
        "v2_response_schema_fingerprint": preflight["v2_response_schema_fingerprint"],
        "complete_request_fingerprint": preflight["complete_request_fingerprint"],
        "packet_fingerprint": preflight["packet_fingerprint"],
        "ordered_crops_fingerprint": preflight["ordered_crops_fingerprint"],
        "prompt_fingerprint": preflight["prompt_fingerprint"],
        "frozen_semantic_schema_fingerprint": (
            preflight["frozen_semantic_schema_fingerprint"]
        ),
        "schema_family_fingerprint": preflight["schema_family_fingerprint"],
        "packet_specific_schema_fingerprint": (
            preflight["packet_specific_schema_fingerprint"]
        ),
        "ordered_crop_label_image_framing_fingerprint": (
            preflight["ordered_crop_label_image_framing_fingerprint"]
        ),
        "generation_settings_v2_fingerprint": (
            preflight["generation_settings_v2_fingerprint"]
        ),
        "planner_fingerprint": preflight["planner_fingerprint"],
        "serialization_version": preflight["serialization_version"],
        "crop_framing_version": preflight["crop_framing_version"],
        "maximum_requests": MAXIMUM_REQUESTS,
        "retry_count": 0,
        "fallback_attempts": 0,
        "provider_requests": 0,
    }
    if not args.execute:
        print(json.dumps(preflight_summary, indent=2, sort_keys=True))
        return 0

    if preflight["schema_family_fingerprint"] != AUTHORIZED_SCHEMA_FAMILY_SHA256:
        raise SystemExit("evidence_linkage_smoke_requires_separate_authorization")

    api_key = next((
        str(os.environ.get(name) or "").strip()
        for name in ("GEMINI_API_KEY", "AI_VISION_API_KEY", "AI_API_KEY")
        if str(os.environ.get(name) or "").strip()
    ), "")
    if not api_key:
        raise SystemExit("contract_smoke_private_credential_unavailable")

    smoke_root = experiment_root / "phase_a" / "supplementary_transport_v2_smoke"
    smoke_root.mkdir(parents=True, exist_ok=True)
    _assert_one_shot_available(smoke_root)
    run_root = smoke_root / _run_id()
    run_root.mkdir(parents=False, exist_ok=False)
    state_path = run_root / "smoke_state.json"
    state: dict[str, Any] = {
        **preflight_summary,
        "status": "prepared",
        "authorization_consumed": False,
        "bundle_sha256_before": preflight["bundle_tree_sha256"],
        "bundle_sha256_after": None,
        "request": None,
        "contract_result": None,
        "raw_provider_response_persisted": False,
        "credentials_or_headers_persisted": False,
        "private_artifacts_git_ignored": True,
    }
    _atomic_json(state_path, state)

    spend = ExperimentSpendController(
        experiment_root, "exp-document-learning-simulation",
    )
    snapshot = spend.snapshot("A")
    if snapshot.canceled:
        raise SystemExit("phase_a_spend_ledger_canceled")
    remaining = (
        Decimal(snapshot.current_phase_cap_usd)
        - Decimal(snapshot.cumulative_charged_usd)
        - Decimal(snapshot.active_reserved_usd)
    )
    if remaining < MAXIMUM_RESERVATION_USD:
        raise SystemExit("phase_a_budget_insufficient")
    reservation = spend.reserve(
        phase="A",
        estimated_cost_usd=MAXIMUM_RESERVATION_USD,
        provider="gemini",
        model_id=ARM_A_MODEL_ID,
        profile_id="phase-a-private-contract-smoke",
        stage="controlled_private_supplementary_contract_smoke",
        document_sha256=record.packet_sha256,
        purpose="frozen_packet_direct_v2_contract_validation_one_shot",
    )

    initial_facts = _safe_initial_facts(preflight)
    state["authorization_consumed"] = True
    state["status"] = "dispatching"
    state["provider_requests"] = 1
    _atomic_json(state_path, state)

    http_status: int | None = None
    provider_envelope: object = None
    safe_status: str | None = None
    safe_code: int | None = None
    safe_category: str | None = None
    failure_code: str | None = None
    started = time.perf_counter()
    try:
        request = urllib.request.Request(
            preflight["endpoint"],
            data=json.dumps(
                preflight["payload"], separators=(",", ":"),
            ).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        reservation = spend.mark_dispatched(reservation.reservation_id)
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=150) as response:
            http_status = int(response.status)
            raw_body = response.read(500_000)
        provider_envelope = json.loads(raw_body.decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        http_status = int(exc.code)
        safe_status, safe_code, safe_category = _safe_provider_error(exc.read(4096))
        failure_code = f"http_{http_status}"
    except (urllib.error.URLError, ContractSmokeError):
        safe_category = "provider_transport_unavailable"
        failure_code = "provider_transport_unavailable"
    except (TypeError, ValueError, json.JSONDecodeError):
        safe_category = "provider_response_invalid_json"
        failure_code = "provider_response_invalid_json"
    latency_ms = round((time.perf_counter() - started) * 1000, 3)

    raw_text, finish_reason, returned_model = _native_text(provider_envelope)
    usage, thinking_tokens = _usage(provider_envelope)
    usage_reported = bool(any(usage.values()))
    exact_model_match = returned_model == ARM_A_MODEL_ID
    if http_status == 200 and not exact_model_match:
        failure_code = "provider_model_mismatch"
        safe_category = failure_code
    actual_cost = (
        _actual_cost(
            arm=ExperimentArm.A,
            usage=usage,
            thinking_tokens=thinking_tokens,
        )
        if usage_reported else None
    )
    reservation = spend.settle(
        reservation.reservation_id,
        actual_cost_usd=actual_cost,
        usage={
            "input_tokens": usage["input_tokens"],
            "visible_output_tokens": usage["visible_output_tokens"],
            "thinking_tokens": int(thinking_tokens or 0),
            "total_tokens": usage["total_tokens"],
            "provider_request_count": 1,
        },
        provider_reported_usage=usage_reported,
        failure_code=failure_code,
    )
    cost_view = spend_cost_accounting_view(reservation)
    state["request"] = {
        "provider": "gemini",
        "model_id": ARM_A_MODEL_ID,
        "endpoint_host": AUTHORIZED_HOST,
        "http_status": http_status,
        "exact_model_match": exact_model_match,
        "finish_reason": finish_reason,
        "latency_ms": latency_ms,
        "input_tokens": usage["input_tokens"] if usage_reported else None,
        "visible_output_tokens": (
            usage["visible_output_tokens"] if usage_reported else None
        ),
        "thinking_tokens": thinking_tokens,
        "safe_error_status": safe_status,
        "safe_error_code": safe_code,
        "safe_error_category": safe_category,
        **cost_view.model_dump(),
        "retry_count": 0,
        "fallback_attempts": 0,
        "raw_response_persisted": False,
    }

    if (
        http_status == 200
        and exact_model_match
        and usage_reported
        and actual_cost is not None
    ):
        state["contract_result"] = _safe_contract_result(
            preflight=preflight,
            raw_text=raw_text,
            initial_facts=initial_facts,
            finish_reason=finish_reason,
        )
    else:
        state["contract_result"] = {
            "canonical_outcome": (
                failure_code or "provider_usage_or_verified_cost_unavailable"
            ),
            "merge_executed": False,
            "reconciliation_state": "not_run",
            "final_disposition": "blocked",
            "accepted": False,
            "export_allowed": False,
            "false_safe_export": False,
        }
    raw_text = None
    provider_envelope = None

    state["bundle_sha256_after"] = _tree_sha256(preflight["bundle_root"])
    if state["bundle_sha256_after"] != state["bundle_sha256_before"]:
        state["contract_result"]["canonical_outcome"] = "frozen_bundle_mutated"
        state["contract_result"]["final_disposition"] = "blocked"
    state["status"] = "persisting_terminal_disposition"
    state["terminal_disposition_count"] = 0
    state["terminal_disposition_persisted"] = False
    _atomic_json(state_path, state)

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    persisted_disposition = (
        persisted.get("contract_result", {}).get("final_disposition")
    )
    state["terminal_disposition_persisted"] = bool(
        persisted_disposition in TECHNICAL_SMOKE_DISPOSITIONS
    )
    state["terminal_disposition_count"] = int(
        state["terminal_disposition_persisted"]
    )
    state["technical_acceptance"] = _technical_smoke_acceptance(
        state["contract_result"],
        terminal_disposition_persisted=state["terminal_disposition_persisted"],
        terminal_disposition_count=state["terminal_disposition_count"],
    )
    if state["bundle_sha256_after"] != state["bundle_sha256_before"]:
        state["status"] = "terminated_fail_closed"
    elif not usage_reported or actual_cost is None:
        state["status"] = "terminated_fail_closed"
    elif state["technical_acceptance"]["terminal_action"] == (
        "stop_evidence_validation_failed"
    ):
        state["status"] = "stopped_evidence_validation_failed"
    elif state["technical_acceptance"]["technical_success"]:
        state["status"] = "completed"
    else:
        state["status"] = "failed_technical_acceptance"
    _atomic_json(state_path, state)

    safe_output = {
        "status": state["status"],
        "selected_packet_fingerprint": record.packet_sha256,
        "provider": "gemini",
        "model_id": ARM_A_MODEL_ID,
        "host": AUTHORIZED_HOST,
        "request_count": 1,
        "request": state["request"],
        "contract_result": state["contract_result"],
        "technical_acceptance": state["technical_acceptance"],
        "terminal_disposition_persisted": state["terminal_disposition_persisted"],
        "terminal_disposition_count": state["terminal_disposition_count"],
        "bundle_hash_unchanged": (
            state["bundle_sha256_after"] == state["bundle_sha256_before"]
        ),
        "raw_provider_response_persisted": False,
        "credentials_or_headers_persisted": False,
    }
    print(json.dumps(safe_output, indent=2, sort_keys=True))
    return 0 if state["technical_acceptance"]["technical_success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
