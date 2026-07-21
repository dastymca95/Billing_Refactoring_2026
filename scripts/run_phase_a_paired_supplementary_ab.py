"""Execute the one authorized private paired supplementary A/B experiment.

The runner consumes the existing immutable five-packet bundle exactly once per
model.  It has no extraction, planning, rendering, retry, fallback, accounting,
readiness, or export route.  Raw provider responses are never persisted.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.review_phase_a_supplementary_evidence import (  # noqa: E402
    _assert_private_output_root,
    _batch_id_for_source,
    _experiment_root,
    _field_for_target,
    _find_saved_observed_facts,
)
from webapp.backend.services.accounting_contracts import (  # noqa: E402
    DocumentFacts,
    EvidenceReference,
    LineItemFacts,
)
from webapp.backend.services.experiment_spend_controller import (  # noqa: E402
    ExperimentSpendController,
    spend_cost_accounting_view,
)
from webapp.backend.services.gemini_supplementary_transport import (  # noqa: E402
    SUPPLEMENTARY_TRANSPORT_V2_VERSION,
    parse_supplementary_transport_v2_response_with_audit,
    supplementary_transport_v2_response_format,
)
from webapp.backend.services.gemini_supplementary_verification import (  # noqa: E402
    SupplementaryTarget,
    SupplementaryTargetType,
    SupplementaryVerificationError,
    merge_supplementary_observations,
    reconciliation_snapshot,
    supplementary_response_format,
    validate_observation_crop_references,
)
from webapp.backend.services.supplementary_ab_arm_b_candidate import (  # noqa: E402
    ARM_A_INPUT_PRICE_USD_PER_MILLION,
    ARM_A_MODEL_ID,
    ARM_A_OUTPUT_PRICE_USD_PER_MILLION,
    ARM_B_CANDIDATE_MODEL_ID,
    ARM_B_INPUT_PRICE_USD_PER_MILLION,
    ARM_B_OUTPUT_PRICE_USD_PER_MILLION,
    PREVIOUS_ARM_B_STATUS,
    PreparedArmBExecutionOverlay,
    calculate_candidate_budget,
)
from webapp.backend.services.supplementary_ab_experiment import (  # noqa: E402
    AB_FREEZE_VERSION,
    AB_SERIALIZATION_VERSION,
    ABContractError,
    ExperimentArm,
    FrozenPairedManifest,
    OneShotPairedLedger,
    PacketOutcome,
    canonical_json_bytes,
    canonical_json_sha256,
    load_verified_packet_material,
    sha256_bytes,
)
from webapp.backend.services.supplementary_crop_framing import (  # noqa: E402
    AuthorizedCropDescriptor,
    build_supplementary_crop_framing,
    evidence_linkage_instruction,
)


AUTHORIZED_HOST = "generativelanguage.googleapis.com"
BUNDLE_NAME = "paired-bundle-v1"
MAX_REQUESTS = 10
MAX_PER_PACKET_ARM = 1
MAX_OUTPUT_TOKENS = 2048
AB_SUB_CAP_USD = Decimal("1.00")
EXPECTED_INPUT_TOKENS_PER_ARM = 27_507
EXPECTED_OUTPUT_TOKENS_PER_ARM = 3_719
FROZEN_SETTINGS = {
    "temperature": 0,
    "max_output_tokens": 2048,
    "candidate_count": 1,
    "response_mime_type": "application/json",
    "retry_count": 0,
    "fallback": False,
}


class PairedExecutionFailure(RuntimeError):
    pass


def _now_id() -> str:
    return datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S%fZ")


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _safe_provider_error(raw: bytes) -> tuple[str | None, int | None, str]:
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return None, None, "provider_error_unclassified"
        status = str(error.get("status") or "")[:80] or None
        try:
            code = int(error.get("code")) if error.get("code") is not None else None
        except (TypeError, ValueError):
            code = None
        if status in {"UNAUTHENTICATED", "PERMISSION_DENIED"} or code in {401, 403}:
            category = "authentication_or_permission_failed"
        elif status == "INVALID_ARGUMENT" or code == 400:
            category = "native_request_contract_invalid"
        elif status == "UNAVAILABLE" or code == 503:
            category = "provider_temporarily_unavailable"
        elif status == "RESOURCE_EXHAUSTED" or code == 429:
            category = "provider_rate_limited"
        else:
            category = "provider_error_unclassified"
        return status, code, category
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None, "provider_error_unclassified"


def _native_text(envelope: object) -> tuple[str | None, str | None, str]:
    if not isinstance(envelope, dict):
        return None, None, ""
    candidates = envelope.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        return None, None, str(envelope.get("modelVersion") or "")
    candidate = candidates[0] if isinstance(candidates[0], dict) else {}
    finish = str(candidate.get("finishReason") or "")[:80] or None
    content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    texts = [
        str(part.get("text"))
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return ("".join(texts) if texts else None), finish, str(envelope.get("modelVersion") or "")


def _usage(envelope: object) -> tuple[dict[str, int], int | None]:
    raw = envelope.get("usageMetadata") if isinstance(envelope, dict) else None
    raw = raw if isinstance(raw, dict) else {}

    def integer(name: str) -> int:
        try:
            return max(0, int(raw.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    thinking = integer("thoughtsTokenCount") if raw.get("thoughtsTokenCount") is not None else None
    usage = {
        "input_tokens": integer("promptTokenCount"),
        "visible_output_tokens": integer("candidatesTokenCount"),
        "total_tokens": integer("totalTokenCount"),
    }
    if not usage["total_tokens"]:
        usage["total_tokens"] = (
            usage["input_tokens"]
            + usage["visible_output_tokens"]
            + int(thinking or 0)
        )
    return usage, thinking


def _actual_cost(
    *, arm: ExperimentArm, usage: Mapping[str, int], thinking_tokens: int | None,
) -> Decimal:
    if arm is ExperimentArm.A:
        input_rate = ARM_A_INPUT_PRICE_USD_PER_MILLION
        output_rate = ARM_A_OUTPUT_PRICE_USD_PER_MILLION
    else:
        input_rate = ARM_B_INPUT_PRICE_USD_PER_MILLION
        output_rate = ARM_B_OUTPUT_PRICE_USD_PER_MILLION
    output = int(usage["visible_output_tokens"]) + int(thinking_tokens or 0)
    return (
        Decimal(int(usage["input_tokens"])) * input_rate / Decimal(1_000_000)
        + Decimal(output) * output_rate / Decimal(1_000_000)
    ).quantize(Decimal("0.000001"))


def _target_for_record(record: Any) -> SupplementaryTarget:
    value = str(record.target_category)
    return SupplementaryTarget(
        target_type=SupplementaryTargetType(value),
        page_number=1,
        field_name=_field_for_target(value),
        local_trigger_codes=["paired_ab_frozen_target"],
    )


def _request_payload(
    *, arm: ExperimentArm, prompt: bytes, schema: bytes,
    record: Any, crops: tuple[bytes, ...],
) -> tuple[str, dict[str, Any], str]:
    model = ARM_A_MODEL_ID if arm is ExperimentArm.A else ARM_B_CANDIDATE_MODEL_ID
    endpoint = f"https://{AUTHORIZED_HOST}/v1beta/models/{model}:generateContent"
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or parsed.hostname != AUTHORIZED_HOST:
        raise PairedExecutionFailure("unauthorized_provider_host")

    target = _target_for_record(record)
    frozen_schema = json.loads(schema.decode("utf-8"))
    if canonical_json_bytes(frozen_schema) != schema:
        raise PairedExecutionFailure("frozen_schema_not_canonical")
    if frozen_schema != supplementary_response_format(target):
        raise PairedExecutionFailure("frozen_semantic_schema_mismatch")
    planned_crops = {
        item.crop_id: {
            "role": item.role,
            "ordinal": item.ordinal,
            "target_relevance": f"{target.target_type.value}:{item.category}",
            "mime_type": item.mime_type,
            "plan_id": getattr(record, "plan_id", None),
            "packet_sha256": record.packet_sha256,
            "source_kind": "frozen_supplementary_crop",
        }
        for item in record.crops
    }
    wire = supplementary_transport_v2_response_format(
        target, planned_crops=planned_crops,
    )["json_schema"]["schema"]
    descriptors = tuple(
        AuthorizedCropDescriptor(
            crop_id=item.crop_id,
            crop_role=item.role,
            ordinal=item.ordinal,
            target_relevance=f"{target.target_type.value}:{item.category}",
            mime_type=item.mime_type,
            source_kind="frozen_supplementary_crop",
        )
        for item in record.crops
    )
    framing = build_supplementary_crop_framing(
        descriptors=descriptors,
        images=crops,
        schema=wire,
        packet_sha256=record.packet_sha256,
        transport_version=SUPPLEMENTARY_TRANSPORT_V2_VERSION,
    )
    parts: list[dict[str, Any]] = [
        {"text": prompt.decode("utf-8")},
        {"text": evidence_linkage_instruction(descriptors)},
        *[dict(item) for item in framing.parts],
    ]
    generation: dict[str, Any] = {
        "responseMimeType": "application/json",
        "responseJsonSchema": wire,
        "maxOutputTokens": MAX_OUTPUT_TOKENS,
        "candidateCount": 1,
    }
    if arm is ExperimentArm.A:
        generation["temperature"] = 0
    else:
        generation["thinkingConfig"] = {"thinkingLevel": "minimal"}
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": generation,
    }
    semantic_fingerprint = sha256_bytes(
        prompt + b"\n" + schema + b"\n" + b"".join(crops)
    )
    return endpoint, payload, semantic_fingerprint


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _evidence_reference(value: Mapping[str, Any], document_id: str) -> EvidenceReference | None:
    source_type = str(value.get("source_type") or "").strip()
    extraction_method = str(value.get("extraction_method") or "").strip()
    if not source_type or not extraction_method:
        return None
    try:
        return EvidenceReference(
            document_id=document_id,
            page=value.get("page"),
            text=value.get("text"),
            normalized_text=value.get("normalized_text"),
            bbox=value.get("bbox"),
            source_type=source_type,
            extraction_method=extraction_method,
            confidence=value.get("confidence"),
        )
    except Exception:
        return None


def _strict_document_facts(
    facts: Mapping[str, Any], *, opaque_document_id: str, model_id: str,
) -> bool:
    rows = [item for item in facts.get("line_items") or [] if isinstance(item, Mapping)]
    if not rows:
        return False
    line_items: list[LineItemFacts] = []
    for index, row in enumerate(rows, 1):
        evidence = [
            candidate
            for value in row.get("evidence") or []
            if isinstance(value, Mapping)
            and (candidate := _evidence_reference(value, opaque_document_id)) is not None
        ]
        try:
            line_items.append(LineItemFacts(
                line_item_id=str(row.get("line_item_id") or row.get("row_label") or index),
                raw_activity=row.get("activity") or row.get("raw_activity"),
                raw_description=row.get("raw_description") or row.get("description"),
                normalized_activity=row.get("normalized_activity"),
                normalized_description=row.get("normalized_description"),
                generated_description=row.get("generated_description"),
                quantity=_decimal(row.get("quantity")),
                unit_price=_decimal(row.get("unit_price")),
                amount=_decimal(row.get("amount")),
                tax=_decimal(row.get("tax")),
                detected_location=row.get("location_candidate") or row.get("detected_location"),
                evidence=evidence,
            ))
        except Exception:
            return False
    document_evidence = [
        candidate
        for value in facts.get("evidence") or []
        if isinstance(value, Mapping)
        and (candidate := _evidence_reference(value, opaque_document_id)) is not None
    ]
    try:
        DocumentFacts(
            document_id=opaque_document_id,
            invoice_id=str(facts.get("invoice_number") or opaque_document_id),
            vendor_candidate=facts.get("vendor_name") or facts.get("vendor_candidate"),
            invoice_number=facts.get("invoice_number"),
            service_address=facts.get("service_address"),
            property_candidate=facts.get("property_candidate"),
            total_amount=_decimal(facts.get("total_amount")),
            document_family_candidate=facts.get("document_family_candidate"),
            line_items=line_items,
            extraction_route="paired_supplementary_ab_replay",
            extraction_model=model_id,
            evidence=document_evidence,
        )
    except Exception:
        return False
    return True


def _provenance_exists(facts: Mapping[str, Any]) -> bool:
    if facts.get("evidence"):
        return True
    return any(
        isinstance(item, Mapping) and bool(item.get("evidence"))
        for item in facts.get("line_items") or []
    ) or bool(facts.get("supplementary_evidence_revisions"))


def _reconciliation_name(snapshot: Mapping[str, Any]) -> str:
    if snapshot.get("reconciled"):
        return "reconciled"
    if snapshot.get("difference") is None:
        return "inconclusive"
    return "unreconciled"


def _evaluate(
    *, record: Any, arm: ExperimentArm, model_id: str,
    initial_facts: Mapping[str, Any], raw_text: str | None,
    finish_reason: str | None, latency_ms: float,
    usage: Mapping[str, int], thinking_tokens: int | None,
    actual_cost: Decimal, provider_schema_available: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    target = _target_for_record(record)
    before = reconciliation_snapshot(initial_facts)
    initial_strict = _strict_document_facts(
        initial_facts, opaque_document_id=record.packet_id, model_id=model_id,
    )
    observation = None
    schema_valid = False
    failure_code = None
    safe_schema_diagnostics = None
    effective = copy.deepcopy(dict(initial_facts))
    if provider_schema_available and raw_text:
        try:
            planned_crops = {
                item.crop_id: {
                    "role": item.role,
                    "ordinal": item.ordinal,
                    "page_number": target.page_number,
                    "plan_id": record.plan_id,
                    "packet_sha256": record.packet_sha256,
                    "source_kind": "frozen_supplementary_crop",
                    "target_relevance": (
                        f"{target.target_type.value}:{item.category}"
                    ),
                }
                for item in record.crops
            }
            parsed_transport = parse_supplementary_transport_v2_response_with_audit(
                raw_text,
                target=target,
                planned_crops=planned_crops,
                plan_id=record.plan_id,
                packet_sha256=record.packet_sha256,
            )
            observation = parsed_transport.observation
            safe_schema_diagnostics = parsed_transport.diagnostics.model_dump(mode="json")
            validate_observation_crop_references(
                observation,
                allowed_crop_ids={item.crop_id for item in record.crops},
                planned_crops=planned_crops,
                expected_packet_sha256=record.packet_sha256,
                actual_packet_sha256=record.packet_sha256,
            )
            schema_valid = True
            effective = merge_supplementary_observations(
                initial_facts, [(target, observation)],
            )
        except SupplementaryVerificationError as exc:
            failure_code = exc.failure_code
            if exc.diagnostics is not None:
                safe_schema_diagnostics = exc.diagnostics.model_dump(mode="json")
    else:
        failure_code = (
            "supplementary_output_truncated" if finish_reason == "MAX_TOKENS"
            else "supplementary_provider_response_unavailable"
        )
    after = reconciliation_snapshot(effective)
    contradiction = bool(observation and observation.contradiction_flag)
    unresolved = bool(not schema_valid or (observation and observation.unresolved_flag))
    if not schema_valid:
        outcome = PacketOutcome.INVALID
    elif contradiction:
        outcome = PacketOutcome.CONTRADICTION
    elif unresolved:
        outcome = PacketOutcome.UNRESOLVED
    else:
        outcome = PacketOutcome.RESOLVED
    visible_candidates = 0
    if observation is not None:
        visible_candidates = len(observation.observed_candidates)
        visible_candidates += int(observation.observed_candidate_value is not None)
    after_strict = _strict_document_facts(
        effective, opaque_document_id=record.packet_id, model_id=model_id,
    )
    provenance = _provenance_exists(effective)
    needs_review = bool(effective.get("needs_manual_review"))
    accepted = bool(
        outcome is PacketOutcome.RESOLVED
        and after_strict
        and provenance
        and bool(after.get("reconciled"))
        and not needs_review
    )
    disposition = "accepted" if accepted else (
        "blocked" if outcome is PacketOutcome.INVALID else "review_required"
    )
    evaluation = {
        "packet_id": record.packet_id,
        "packet_sha256": record.packet_sha256,
        "arm": arm.value,
        "model_id": model_id,
        "schema_valid": schema_valid,
        "outcome": outcome.value,
        "resolved": outcome is PacketOutcome.RESOLVED,
        "unresolved": unresolved,
        "contradiction": contradiction,
        "visible_candidate_count": visible_candidates,
        "resolved_candidate_count": visible_candidates if outcome is PacketOutcome.RESOLVED else 0,
        "reconciliation_before": _reconciliation_name(before),
        "reconciliation_after": _reconciliation_name(after),
        "reconciliation_delta_before": before.get("difference"),
        "reconciliation_delta_after": after.get("difference"),
        "strict_document_facts_before": initial_strict,
        "strict_document_facts_after": after_strict,
        "strict_document_facts_recovered": bool(after_strict and not initial_strict),
        "provenance_intact": provenance,
        "final_disposition": disposition,
        "accepted": accepted,
        "export_allowed": False,
        "false_safe_export": False,
        "latency_ms": latency_ms,
        "input_tokens": int(usage["input_tokens"]),
        "visible_output_tokens": int(usage["visible_output_tokens"]),
        "thinking_tokens": thinking_tokens,
        "actual_provider_cost_usd": str(actual_cost),
        "finish_reason": finish_reason,
        "failure_code": failure_code,
        "safe_schema_diagnostics": safe_schema_diagnostics,
    }
    observation_payload = observation.model_dump(mode="json") if observation is not None else None
    return evaluation, observation_payload


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * fraction
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _aggregate(rows: list[Mapping[str, Any]], arm: ExperimentArm) -> dict[str, Any]:
    selected = [row for row in rows if row.get("arm") == arm.value]
    count = len(selected)
    resolved = sum(bool(row.get("resolved")) for row in selected)
    contradictions = sum(bool(row.get("contradiction")) for row in selected)
    recovered = sum(bool(row.get("strict_document_facts_recovered")) for row in selected)
    accepted = sum(bool(row.get("accepted")) for row in selected)
    review = sum(row.get("final_disposition") == "review_required" for row in selected)
    costs = sum((Decimal(str(row.get("actual_provider_cost_usd") or 0)) for row in selected), Decimal("0"))
    latencies = [float(row.get("latency_ms") or 0) for row in selected]
    tokens = sum(
        int(row.get("input_tokens") or 0)
        + int(row.get("visible_output_tokens") or 0)
        + int(row.get("thinking_tokens") or 0)
        for row in selected
    )

    def ratio(value: int) -> float:
        return value / count if count else 0.0

    def per(denominator: int) -> str | None:
        return str((costs / denominator).quantize(Decimal("0.000001"))) if denominator else None

    return {
        "arm": arm.value,
        "model_id": ARM_A_MODEL_ID if arm is ExperimentArm.A else ARM_B_CANDIDATE_MODEL_ID,
        "request_count": count,
        "supplementary_resolution_rate": ratio(resolved),
        "resolved_count": resolved,
        "contradiction_rate": ratio(contradictions),
        "contradiction_count": contradictions,
        "document_facts_recovery_rate": ratio(recovered),
        "document_facts_recovered_count": recovered,
        "accepted_document_count": accepted,
        "review_required_count": review,
        "false_safe_exports": sum(bool(row.get("false_safe_export")) for row in selected),
        "latency_p50_ms": round(statistics.median(latencies), 3) if latencies else 0,
        "latency_p95_ms": round(_percentile(latencies, 0.95), 3),
        "total_tokens": tokens,
        "total_verified_cost_usd": str(costs.quantize(Decimal("0.000001"))),
        "cost_per_resolved_target_usd": per(resolved),
        "cost_per_recovered_document_facts_usd": per(recovered),
        "cost_per_document_moved_out_of_review_usd": per(accepted),
    }


def _preflight(experiment_root: Path) -> dict[str, Any]:
    _assert_private_output_root(experiment_root)
    bundle_root = (
        experiment_root / "phase_a" / "supplementary_ab" / BUNDLE_NAME
    ).resolve(strict=True)
    complete = (bundle_root / "COMPLETE").read_text(encoding="utf-8").strip()
    if complete != AB_FREEZE_VERSION:
        raise PairedExecutionFailure("frozen_bundle_complete_marker_invalid")
    payload = json.loads((bundle_root / "paired_manifest.json").read_text(encoding="utf-8"))
    manifest = FrozenPairedManifest.model_validate(payload.get("manifest"))
    if len(manifest.packet_records) != 5 or len(manifest.excluded_localization_targets) != 2:
        raise PairedExecutionFailure("frozen_assignment_count_invalid")
    if manifest.schema_version != AB_FREEZE_VERSION:
        raise PairedExecutionFailure("frozen_bundle_schema_version_invalid")
    if any(item.serialization_version != AB_SERIALIZATION_VERSION for item in manifest.packet_records):
        raise PairedExecutionFailure("frozen_serialization_version_mismatch")
    expected_settings = canonical_json_sha256(FROZEN_SETTINGS)
    if any(item.generation_settings_fingerprint != expected_settings for item in manifest.packet_records):
        raise PairedExecutionFailure("frozen_generation_settings_fingerprint_mismatch")
    if any(
        item.reason != "supplementary_evidence_localization_unavailable"
        or item.disposition != "review_required"
        or item.provider_calls != 0
        or item.provider_cost_usd != 0
        or item.accepted
        or item.export_allowed
        for item in manifest.excluded_localization_targets
    ):
        raise PairedExecutionFailure("excluded_localization_contract_changed")

    overlay_path = experiment_root / "telemetry" / "prepared_arm_b_gemini_3_flash_preview.json"
    overlay = PreparedArmBExecutionOverlay.model_validate_json(
        overlay_path.read_text(encoding="utf-8")
    )
    if (
        not overlay.technically_eligible
        or overlay.arm_b_candidate_model_id != ARM_B_CANDIDATE_MODEL_ID
        or overlay.previous_arm_b_status != PREVIOUS_ARM_B_STATUS
        or overlay.bundle_reference != BUNDLE_NAME
        or overlay.bundle_opened_during_preparation
    ):
        raise PairedExecutionFailure("arm_b_candidate_overlay_invalid")

    packet_material: dict[str, tuple[bytes, bytes, bytes, tuple[bytes, ...]]] = {}
    semantic_fingerprints: dict[str, str] = {}
    for record in manifest.packet_records:
        material = load_verified_packet_material(bundle_root, record)
        packet, prompt, schema, crops = material
        if sha256_bytes(packet) != record.packet_sha256:
            raise PairedExecutionFailure("packet_hash_mismatch")
        if tuple(sha256_bytes(item) for item in crops) != tuple(item.sha256 for item in record.crops):
            raise PairedExecutionFailure("ordered_crop_hash_mismatch")
        if [item.ordinal for item in record.crops] != list(range(len(record.crops))):
            raise PairedExecutionFailure("crop_order_mismatch")
        refs = [item for item in manifest.arm_references if item.packet_id == record.packet_id]
        if len(refs) != 2:
            raise PairedExecutionFailure("paired_arm_reference_missing")
        identity = {
            (
                item.packet_sha256, item.prompt_sha256, item.schema_sha256,
                item.ordered_crop_sha256s, item.target_subtype,
                item.generation_settings_fingerprint,
            )
            for item in refs
        }
        if len(identity) != 1:
            raise PairedExecutionFailure("paired_arm_bytes_not_identical")
        _, _, semantic_a = _request_payload(
            arm=ExperimentArm.A, prompt=prompt, schema=schema, record=record, crops=crops,
        )
        _, _, semantic_b = _request_payload(
            arm=ExperimentArm.B, prompt=prompt, schema=schema, record=record, crops=crops,
        )
        if semantic_a != semantic_b:
            raise PairedExecutionFailure("paired_semantic_input_mismatch")
        packet_material[record.packet_id] = material
        semantic_fingerprints[record.packet_id] = semantic_a

    if experiment_root.is_relative_to(PROJECT_ROOT):
        relative = experiment_root.relative_to(PROJECT_ROOT)
        check = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", str(relative)],
            cwd=PROJECT_ROOT, check=False,
        )
        if check.returncode != 0:
            raise PairedExecutionFailure("private_experiment_root_not_ignored")
    return {
        "bundle_root": bundle_root,
        "bundle_tree_sha256": _tree_sha256(bundle_root),
        "manifest": manifest,
        "material": packet_material,
        "semantic_fingerprints": semantic_fingerprints,
        "source_run": (
            experiment_root / "phase_a" / "runs" / manifest.source_run_id
        ).resolve(strict=True),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-root", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    experiment_root = _experiment_root(args.experiment_root)
    preflight = _preflight(experiment_root)
    manifest: FrozenPairedManifest = preflight["manifest"]

    spend = ExperimentSpendController(experiment_root, "exp-document-learning-simulation")
    snapshot = spend.snapshot("A")
    if snapshot.canceled:
        raise SystemExit("paired_ab_spend_ledger_canceled")
    if Decimal(snapshot.cumulative_charged_usd) != Decimal("0.328476"):
        raise SystemExit("paired_ab_authorized_cumulative_spend_mismatch")
    budget = calculate_candidate_budget(
        expected_input_tokens_per_arm=EXPECTED_INPUT_TOKENS_PER_ARM,
        expected_output_tokens_per_arm=EXPECTED_OUTPUT_TOKENS_PER_ARM,
        phase_a_cumulative_spend_usd=Decimal(snapshot.cumulative_charged_usd),
    )
    if budget.maximum_reserved_total_usd > AB_SUB_CAP_USD:
        raise SystemExit("paired_ab_subcap_insufficient")
    if budget.maximum_reserved_total_usd != Decimal("0.087341"):
        raise SystemExit("paired_ab_evidence_reservation_changed")

    preflight_summary = {
        "status": "offline_preflight_valid",
        "packet_count": len(manifest.packet_records),
        "paired_request_limit": MAX_REQUESTS,
        "excluded_target_count": len(manifest.excluded_localization_targets),
        "byte_identity_verified": True,
        "semantic_identity_verified": True,
        "bundle_tree_sha256": preflight["bundle_tree_sha256"],
        "previous_arm_b_status": PREVIOUS_ARM_B_STATUS,
        "arm_models": [ARM_A_MODEL_ID, ARM_B_CANDIDATE_MODEL_ID],
        "maximum_reserved_total_usd": str(budget.maximum_reserved_total_usd),
        "phase_a_remaining_after_maximum_usd": str(budget.phase_a_remaining_after_maximum_usd),
        "provider_requests": 0,
    }
    if not args.execute:
        print(json.dumps(preflight_summary, indent=2, sort_keys=True))
        return 0

    api_key = next((
        str(os.environ.get(name) or "").strip()
        for name in ("GEMINI_API_KEY", "AI_VISION_API_KEY", "AI_API_KEY")
        if str(os.environ.get(name) or "").strip()
    ), "")
    if not api_key:
        raise SystemExit("paired_ab_private_credential_unavailable")

    run_root = (
        experiment_root / "phase_a" / "supplementary_ab" / "executions" / _now_id()
    )
    run_root.mkdir(parents=True, exist_ok=False)
    state_path = run_root / "execution_state.json"
    state: dict[str, Any] = {
        "contract_version": "phase-a-private-paired-supplementary-ab/1.0",
        "status": "running",
        "authorization": {
            "packet_count": 5,
            "maximum_requests": 10,
            "models": [ARM_A_MODEL_ID, ARM_B_CANDIDATE_MODEL_ID],
            "authorized_host": AUTHORIZED_HOST,
            "retries": 0,
            "fallback": False,
        },
        "bundle_tree_sha256_before": preflight["bundle_tree_sha256"],
        "bundle_tree_sha256_after": None,
        "byte_identity_verified": True,
        "excluded_targets": [
            {
                "target_id": item.target_id,
                "reason": item.reason,
                "disposition": item.disposition,
                "accepted": item.accepted,
                "export_allowed": item.export_allowed,
                "provider_calls": item.provider_calls,
                "provider_cost_usd": str(item.provider_cost_usd),
            }
            for item in manifest.excluded_localization_targets
        ],
        "requests": [],
        "evaluations": [],
        "observations": [],
        "fatal_stop_reason": None,
    }
    _atomic_json(state_path, state)

    ledger = OneShotPairedLedger(manifest)
    material_by_packet = preflight["material"]
    source_run: Path = preflight["source_run"]
    request_count = 0
    total_reserved = Decimal("0")
    model_budget = {item.arm: item for item in budget.arms}
    fatal: str | None = None

    for position, record in enumerate(manifest.packet_records, 1):
        if fatal:
            break
        batch_id = _batch_id_for_source(
            source_run, record.opaque_source_sha256, position=position,
        )
        initial = _find_saved_observed_facts(
            source_run, record.opaque_source_sha256, batch_id=batch_id,
        )
        initial_facts = copy.deepcopy(initial) if initial is not None else {
            "line_items": [], "evidence": [], "page_reconciliations": [], "warnings": [],
        }
        for arm in (ExperimentArm.A, ExperimentArm.B):
            if fatal:
                break
            if request_count >= MAX_REQUESTS:
                fatal = "paired_global_request_limit_reached"
                break
            if _tree_sha256(preflight["bundle_root"]) != preflight["bundle_tree_sha256"]:
                fatal = "frozen_bundle_mutated_before_dispatch"
                break
            packet, prompt, schema, crops = load_verified_packet_material(
                preflight["bundle_root"], record,
            )
            if sha256_bytes(packet) != record.packet_sha256:
                fatal = "packet_hash_changed_before_dispatch"
                break
            endpoint, payload, semantic_fingerprint = _request_payload(
                arm=arm, prompt=prompt, schema=schema, record=record, crops=crops,
            )
            if semantic_fingerprint != preflight["semantic_fingerprints"][record.packet_id]:
                fatal = "semantic_input_changed_before_dispatch"
                break
            model = ARM_A_MODEL_ID if arm is ExperimentArm.A else ARM_B_CANDIDATE_MODEL_ID
            selected_budget = model_budget[arm]
            reservation_amount = (
                selected_budget.maximum_reserved_usd / Decimal(5)
            ).quantize(Decimal("0.000001"))
            if total_reserved + reservation_amount > AB_SUB_CAP_USD:
                fatal = "paired_ab_subcap_would_be_exceeded"
                break
            reservation = spend.reserve(
                phase="A",
                estimated_cost_usd=reservation_amount,
                provider="gemini",
                model_id=model,
                profile_id=f"phase-a-private-paired-{arm.value}",
                stage="controlled_private_paired_supplementary_ab",
                document_sha256=record.packet_sha256,
                purpose="paired_frozen_packet_one_shot",
            )
            ledger.register(record.packet_id, arm, reservation)
            total_reserved += reservation_amount

            http_status: int | None = None
            envelope: object = None
            safe_status: str | None = None
            safe_code: int | None = None
            safe_category: str | None = None
            failure_code: str | None = None
            started = time.perf_counter()
            try:
                request = urllib.request.Request(
                    endpoint,
                    data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                    headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                    method="POST",
                )
                reservation = spend.mark_dispatched(reservation.reservation_id)
                ledger.consume(record.packet_id, arm)
                request_count += 1
                with urllib.request.urlopen(request, timeout=150) as response:
                    http_status = int(response.status)
                    raw_body = response.read(500_000)
                envelope = json.loads(raw_body.decode("utf-8", "replace"))
            except urllib.error.HTTPError as exc:
                http_status = int(exc.code)
                safe_status, safe_code, safe_category = _safe_provider_error(exc.read(4096))
                failure_code = f"http_{http_status}"
            except urllib.error.URLError:
                safe_category = "provider_transport_unavailable"
                failure_code = "provider_transport_unavailable"
            except (TypeError, ValueError, json.JSONDecodeError):
                safe_category = "provider_response_invalid_json"
                failure_code = "provider_response_invalid_json"
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            raw_text, finish_reason, returned_model = _native_text(envelope)
            usage, thinking_tokens = _usage(envelope)
            usage_reported = bool(any(usage.values()))
            exact_model = returned_model == model
            if http_status == 200 and not exact_model:
                failure_code = "provider_model_mismatch"
                safe_category = failure_code
            actual_cost = (
                _actual_cost(arm=arm, usage=usage, thinking_tokens=thinking_tokens)
                if usage_reported else None
            )
            if raw_text:
                ledger.record_output(record.packet_id, arm, sha256_bytes(raw_text.encode("utf-8")))
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
            request_record = {
                "packet_id": record.packet_id,
                "packet_sha256": record.packet_sha256,
                "arm": arm.value,
                "model_id": model,
                "endpoint_host": AUTHORIZED_HOST,
                "http_status": http_status,
                "exact_model_match": exact_model,
                "finish_reason": finish_reason,
                "latency_ms": latency_ms,
                "input_tokens": usage["input_tokens"] if usage_reported else None,
                "visible_output_tokens": usage["visible_output_tokens"] if usage_reported else None,
                "thinking_tokens": thinking_tokens,
                "safe_error_status": safe_status,
                "safe_error_code": safe_code,
                "safe_error_category": safe_category,
                **cost_view.model_dump(),
                "retry_count": 0,
                "fallback_attempts": 0,
                "raw_response_persisted": False,
            }
            state["requests"].append(request_record)

            if not usage_reported or actual_cost is None:
                fatal = "provider_usage_or_verified_cost_unavailable"
            elif http_status != 200 or not exact_model:
                fatal = failure_code or "provider_request_failed"
            else:
                evaluation, observation = _evaluate(
                    record=record,
                    arm=arm,
                    model_id=model,
                    initial_facts=initial_facts,
                    raw_text=raw_text,
                    finish_reason=finish_reason,
                    latency_ms=latency_ms,
                    usage=usage,
                    thinking_tokens=thinking_tokens,
                    actual_cost=actual_cost,
                    provider_schema_available=bool(raw_text),
                )
                state["evaluations"].append(evaluation)
                if observation is not None:
                    state["observations"].append({
                        "packet_id": record.packet_id,
                        "arm": arm.value,
                        "observation": observation,
                    })
                if evaluation["false_safe_export"]:
                    fatal = "false_safe_export_detected"
                if (
                    (evaluation["unresolved"] or evaluation["contradiction"])
                    and (evaluation["accepted"] or evaluation["export_allowed"])
                ):
                    fatal = "unsafe_unresolved_acceptance_detected"

            state["fatal_stop_reason"] = fatal
            _atomic_json(state_path, state)
            raw_text = None
            envelope = None
            if _tree_sha256(preflight["bundle_root"]) != preflight["bundle_tree_sha256"]:
                fatal = "frozen_bundle_mutated_after_dispatch"
                state["fatal_stop_reason"] = fatal
                _atomic_json(state_path, state)

    state["bundle_tree_sha256_after"] = _tree_sha256(preflight["bundle_root"])
    if state["bundle_tree_sha256_after"] != state["bundle_tree_sha256_before"]:
        fatal = fatal or "frozen_bundle_mutated"
    state["fatal_stop_reason"] = fatal
    state["status"] = "terminated_fail_closed" if fatal else "completed"
    aggregates = {
        arm.value: _aggregate(state["evaluations"], arm)
        for arm in (ExperimentArm.A, ExperimentArm.B)
    }
    state["aggregates"] = aggregates
    arm_a = aggregates[ExperimentArm.A.value]
    arm_b = aggregates[ExperimentArm.B.value]
    state["comparison"] = {
        "resolution_count_delta_b_minus_a": arm_b["resolved_count"] - arm_a["resolved_count"],
        "contradiction_count_delta_b_minus_a": arm_b["contradiction_count"] - arm_a["contradiction_count"],
        "document_facts_recovery_delta_b_minus_a": (
            arm_b["document_facts_recovered_count"] - arm_a["document_facts_recovered_count"]
        ),
        "accepted_document_delta_b_minus_a": (
            arm_b["accepted_document_count"] - arm_a["accepted_document_count"]
        ),
        "review_required_delta_b_minus_a": (
            arm_b["review_required_count"] - arm_a["review_required_count"]
        ),
        "materially_useful": bool(
            arm_b["resolved_count"] >= 3
            and arm_b["resolved_count"] > arm_a["resolved_count"]
            and (
                arm_b["document_facts_recovered_count"]
                + arm_b["accepted_document_count"]
            ) >= 2
            and arm_b["false_safe_exports"] == 0
        ),
    }
    state["provider_hosts_contacted"] = [AUTHORIZED_HOST] if request_count else []
    state["provider_request_count"] = request_count
    state["retry_count"] = 0
    state["fallback_attempts"] = 0
    state["cumulative_phase_a_spend_usd"] = spend.snapshot("A").cumulative_charged_usd
    state["private_artifacts_git_ignored"] = True
    state["raw_provider_responses_persisted"] = False
    state["credentials_or_headers_persisted"] = False
    _atomic_json(state_path, state)

    safe_report = {
        "status": state["status"],
        "fatal_stop_reason": fatal,
        "provider_request_count": request_count,
        "requests_by_arm": dict(Counter(item["arm"] for item in state["requests"])),
        "byte_identity_verified": state["byte_identity_verified"],
        "bundle_unchanged": (
            state["bundle_tree_sha256_before"] == state["bundle_tree_sha256_after"]
        ),
        "evaluations": state["evaluations"],
        "aggregates": aggregates,
        "comparison": state["comparison"],
        "excluded_target_count": len(state["excluded_targets"]),
        "false_safe_exports": sum(
            int(item.get("false_safe_export") or 0) for item in state["evaluations"]
        ),
        "provider_hosts_contacted": state["provider_hosts_contacted"],
        "retry_count": 0,
        "fallback_attempts": 0,
        "cumulative_phase_a_spend_usd": state["cumulative_phase_a_spend_usd"],
        "private_artifacts_git_ignored": True,
        "raw_provider_responses_persisted": False,
        "credentials_or_headers_persisted": False,
    }
    print(json.dumps(safe_report, indent=2, sort_keys=True))
    return 1 if fatal else 0


if __name__ == "__main__":
    raise SystemExit(main())
