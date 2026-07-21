"""Offline-only contract for a fresh paired supplementary-model experiment.

Historical Phase A provider results are deliberately *not* an experiment arm.
Both future arms consume the same newly frozen bytes exactly once.  This module
has no provider client, network, extraction, accounting, or readiness authority.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_UP
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .experiment_spend_controller import (
    ExperimentPhase,
    ExperimentSpendController,
    SpendReservation,
)


AB_CONTRACT_VERSION = "phase-a-paired-supplementary-ab/2.0"
AB_FREEZE_VERSION = "phase-a-paired-supplementary-bundle/1.0"
AB_SERIALIZATION_VERSION = "supplementary-packet-framing/1.0"
AB_PRICING_VERSION = "google-gemini-standard-2026-07-20"
ARM_A_MODEL_ID = "gemini-3.1-flash-lite"
ARM_B_MODEL_ID = "gemini-3.5-flash"
AUTHORIZED_HOST = "generativelanguage.googleapis.com"
ELIGIBLE_PACKET_COUNT = 5
EXCLUDED_LOCALIZATION_TARGET_COUNT = 2
MAX_OUTPUT_TOKENS = 2048
AB_SUB_BUDGET_USD = Decimal("1.00")
PHASE_A_CAP_USD = Decimal("10.00")


class ABContractError(RuntimeError):
    """Fail-closed contract error containing no private values."""


class ExperimentArm(str, Enum):
    A = "arm_a"
    B = "arm_b"


class PacketOutcome(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    CONTRADICTION = "contradiction"
    INVALID = "invalid"


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: ExperimentArm
    provider: str = "gemini"
    model_id: str
    authorized_host: str = AUTHORIZED_HOST
    input_price_usd_per_million: Decimal
    output_price_usd_per_million: Decimal
    pricing_version: str = AB_PRICING_VERSION
    project_availability: str = "operator_confirmation_required"
    image_input_required: bool = True
    structured_output_required: bool = True


def model_profiles() -> tuple[ModelProfile, ModelProfile]:
    return (
        ModelProfile(
            arm=ExperimentArm.A,
            model_id=ARM_A_MODEL_ID,
            input_price_usd_per_million=Decimal("0.25"),
            output_price_usd_per_million=Decimal("1.50"),
        ),
        ModelProfile(
            arm=ExperimentArm.B,
            model_id=ARM_B_MODEL_ID,
            input_price_usd_per_million=Decimal("1.50"),
            output_price_usd_per_million=Decimal("9.00"),
        ),
    )


class FrozenCropReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    crop_id: str
    role: str
    category: str
    ordinal: int = Field(ge=0)
    relative_blob_path: str
    mime_type: str = "image/jpeg"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    pixel_count: int = Field(gt=0)
    byte_length: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_reference(self) -> "FrozenCropReference":
        path = Path(self.relative_blob_path)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("frozen_crop_path_must_be_private_relative")
        if self.pixel_count != self.width * self.height:
            raise ValueError("frozen_crop_pixel_count_mismatch")
        return self


class FrozenPacketRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    packet_id: str
    plan_id: str
    target_category: str
    target_subtype: str
    opaque_source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    relative_packet_path: str
    packet_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    packet_byte_length: int = Field(gt=0)
    total_pixels: int = Field(gt=0)
    crops: tuple[FrozenCropReference, ...]
    relative_prompt_path: str
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_byte_length: int = Field(gt=0)
    relative_schema_path: str
    schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schema_byte_length: int = Field(gt=0)
    generation_settings_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    planner_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    serialization_version: str = AB_SERIALIZATION_VERSION
    offline_regeneration_equal: bool

    @model_validator(mode="after")
    def validate_packet(self) -> "FrozenPacketRecord":
        paths = (
            self.relative_packet_path, self.relative_prompt_path, self.relative_schema_path,
            *(item.relative_blob_path for item in self.crops),
        )
        for value in paths:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError("frozen_packet_path_must_be_private_relative")
        if not self.crops:
            raise ValueError("frozen_packet_requires_crops")
        if [item.ordinal for item in self.crops] != list(range(len(self.crops))):
            raise ValueError("frozen_crop_order_invalid")
        if len({item.crop_id for item in self.crops}) != len(self.crops):
            raise ValueError("frozen_crop_id_duplicate")
        return self


class PairedArmReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: ExperimentArm
    model_id: str
    packet_id: str
    packet_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ordered_crop_sha256s: tuple[str, ...]
    target_subtype: str
    generation_settings_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExcludedLocalizationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str
    target_category: str
    reason: str = "supplementary_evidence_localization_unavailable"
    disposition: str = "review_required"
    provider_calls: int = 0
    provider_cost_usd: Decimal = Decimal("0")
    accepted: bool = False
    export_allowed: bool = False

    @model_validator(mode="after")
    def validate_exclusion(self) -> "ExcludedLocalizationTarget":
        if (
            self.reason != "supplementary_evidence_localization_unavailable"
            or self.disposition != "review_required"
            or self.provider_calls != 0 or self.provider_cost_usd != 0
            or self.accepted or self.export_allowed
        ):
            raise ValueError("localization_target_exclusion_contract_failed")
        return self


class SyntheticCapabilityProbeContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = "gemini"
    model_id: str = ARM_B_MODEL_ID
    authorized_host: str = AUTHORIZED_HOST
    project_availability: str = "operator_confirmation_required"
    synthetic_only: bool = True
    maximum_requests: int = 1
    execute_during_preparation: bool = False
    verify_project_authentication: bool = True
    verify_model_availability: bool = True
    verify_image_input: bool = True
    verify_structured_output: bool = True
    usage_and_cost_reporting_required: bool = True
    response_persistence_allowed: bool = False
    image_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class FrozenPairedManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = AB_FREEZE_VERSION
    source_run_id: str
    source_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    packet_records: tuple[FrozenPacketRecord, ...]
    arm_references: tuple[PairedArmReference, ...]
    excluded_localization_targets: tuple[ExcludedLocalizationTarget, ...]
    capability_probe: SyntheticCapabilityProbeContract
    historical_results_are_formal_arm: bool = False
    external_provider_calls_during_preparation: int = 0

    @model_validator(mode="after")
    def validate_pairing(self) -> "FrozenPairedManifest":
        if self.historical_results_are_formal_arm:
            raise ValueError("historical_result_cannot_be_formal_arm")
        if len(self.packet_records) != ELIGIBLE_PACKET_COUNT:
            raise ValueError("paired_experiment_requires_exactly_five_packets")
        if len(self.excluded_localization_targets) != EXCLUDED_LOCALIZATION_TARGET_COUNT:
            raise ValueError("localization_exclusion_count_mismatch")
        if self.external_provider_calls_during_preparation != 0:
            raise ValueError("offline_preparation_made_provider_call")
        records = {item.packet_id: item for item in self.packet_records}
        if len(records) != ELIGIBLE_PACKET_COUNT:
            raise ValueError("paired_packet_id_duplicate")
        excluded = {item.target_id for item in self.excluded_localization_targets}
        if excluded & records.keys():
            raise ValueError("paired_assignment_includes_excluded_target")
        if len(self.arm_references) != ELIGIBLE_PACKET_COUNT * 2:
            raise ValueError("paired_arm_reference_count_invalid")
        for packet_id, record in records.items():
            refs = [item for item in self.arm_references if item.packet_id == packet_id]
            if {item.arm for item in refs} != {ExperimentArm.A, ExperimentArm.B}:
                raise ValueError("packet_missing_paired_arm")
            expected = (
                record.packet_sha256, record.prompt_sha256, record.schema_sha256,
                tuple(item.sha256 for item in record.crops), record.target_subtype,
                record.generation_settings_fingerprint,
            )
            for ref in refs:
                if (
                    ref.packet_sha256, ref.prompt_sha256, ref.schema_sha256,
                    ref.ordered_crop_sha256s, ref.target_subtype,
                    ref.generation_settings_fingerprint,
                ) != expected:
                    raise ValueError("paired_arms_do_not_reference_identical_bytes")
                expected_model = ARM_A_MODEL_ID if ref.arm is ExperimentArm.A else ARM_B_MODEL_ID
                if ref.model_id != expected_model:
                    raise ValueError("paired_arm_model_mismatch")
        return self


class PairedExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    packets: int = ELIGIBLE_PACKET_COUNT
    arms: int = 2
    maximum_total_requests: int = 10
    maximum_requests_per_packet_per_arm: int = 1
    retries: int = 0
    fallback_enabled: bool = False
    extraction_enabled: bool = False
    crop_regeneration_enabled: bool = False
    reconstruction_enabled: bool = False
    second_supplement_enabled: bool = False
    repair_enabled: bool = False
    other_provider_enabled: bool = False
    temperature: Decimal = Decimal("0")
    max_output_tokens: int = MAX_OUTPUT_TOKENS
    authorized_host: str = AUTHORIZED_HOST


class ModelBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: ExperimentArm
    model_id: str
    expected_input_tokens: int = Field(gt=0)
    expected_output_tokens: int = Field(ge=0)
    maximum_input_tokens: int = Field(gt=0)
    maximum_output_tokens: int = Field(gt=0)
    estimated_cost_usd: Decimal
    maximum_reserved_usd: Decimal
    expected_cost_per_packet_usd: Decimal
    maximum_reservation_per_packet_usd: Decimal


class PairedBudgetEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pricing_version: str = AB_PRICING_VERSION
    packet_count: int = ELIGIBLE_PACKET_COUNT
    model_budgets: tuple[ModelBudget, ...]
    expected_total_usd: Decimal
    maximum_reserved_total_usd: Decimal
    ab_sub_budget_usd: Decimal = AB_SUB_BUDGET_USD
    phase_a_cumulative_spend_usd: Decimal
    phase_a_remaining_before_usd: Decimal
    phase_a_remaining_after_maximum_usd: Decimal
    safety_margin_multiplier: Decimal
    observed_packet_total_pixels: int = Field(gt=0)
    observed_prompt_total_bytes: int = Field(gt=0)
    estimate_basis: str = "same-five-packet-prior-usage-plus-observed-frozen-pixels-and-prompts"

    @model_validator(mode="after")
    def validate_caps(self) -> "PairedBudgetEstimate":
        if self.maximum_reserved_total_usd > self.ab_sub_budget_usd:
            raise ValueError("paired_ab_sub_budget_exceeded")
        if self.phase_a_remaining_after_maximum_usd < 0:
            raise ValueError("phase_a_budget_exceeded")
        return self


class PacketArmEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    packet_id: str
    arm: ExperimentArm
    schema_valid: bool
    outcome: PacketOutcome
    visible_candidate_count: int = Field(ge=0)
    resolved_candidate_count: int = Field(ge=0)
    contradiction: bool
    unresolved: bool
    reconciliation_before: str
    reconciliation_after: str
    reconciliation_delta_before: str | None = None
    reconciliation_delta_after: str | None = None
    strict_document_facts_recovered: bool
    disposition: str
    accepted: bool
    export_allowed: bool
    false_safe_export: bool
    latency_ms: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    actual_cost_usd: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def safety(self) -> "PacketArmEvaluation":
        if (self.unresolved or self.contradiction or not self.schema_valid) and (
            self.accepted or self.export_allowed or self.false_safe_export
        ):
            raise ValueError("unsafe_paired_outcome")
        return self


class PairedAggregateEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: ExperimentArm
    packet_count: int = Field(ge=0)
    resolution_rate: Decimal
    contradiction_rate: Decimal
    document_facts_recovery_rate: Decimal
    accepted_document_increase: int
    review_required_reduction: int
    false_safe_exports: int = Field(ge=0)
    latency_p50_ms: float = Field(ge=0)
    latency_p95_ms: float = Field(ge=0)
    total_cost_usd: Decimal = Field(ge=0)
    incremental_model_cost_usd: Decimal = Field(ge=0)
    cost_per_newly_resolved_target_usd: Decimal | None = None
    cost_per_recovered_document_facts_usd: Decimal | None = None
    cost_per_document_moved_out_of_review_usd: Decimal | None = None


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def frame_packet_bytes(crops: Sequence[tuple[Mapping[str, Any], bytes]]) -> bytes:
    """Length-frame ordered metadata and images into one deterministic packet."""
    output = bytearray(AB_SERIALIZATION_VERSION.encode("ascii") + b"\n")
    output.extend(len(crops).to_bytes(4, "big"))
    for metadata, content in crops:
        header = canonical_json_bytes(dict(metadata))
        output.extend(len(header).to_bytes(8, "big")); output.extend(header)
        output.extend(len(content).to_bytes(8, "big")); output.extend(content)
    return bytes(output)


def load_verified_packet_material(
    bundle_root: Path, record: FrozenPacketRecord,
) -> tuple[bytes, bytes, bytes, tuple[bytes, ...]]:
    root = bundle_root.resolve(strict=True)

    def read(relative: str, expected_hash: str, expected_length: int) -> bytes:
        path = (root / relative).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ABContractError("frozen_material_path_escaped_bundle") from exc
        content = path.read_bytes()
        if len(content) != expected_length or sha256_bytes(content) != expected_hash:
            raise ABContractError("frozen_material_integrity_failure")
        return content

    packet = read(record.relative_packet_path, record.packet_sha256, record.packet_byte_length)
    prompt = read(record.relative_prompt_path, record.prompt_sha256, record.prompt_byte_length)
    schema = read(record.relative_schema_path, record.schema_sha256, record.schema_byte_length)
    crops = tuple(read(c.relative_blob_path, c.sha256, c.byte_length) for c in record.crops)
    framed = frame_packet_bytes(tuple(({
        "crop_id": c.crop_id, "role": c.role, "category": c.category,
        "ordinal": c.ordinal, "mime_type": c.mime_type, "width": c.width,
        "height": c.height, "pixel_count": c.pixel_count, "sha256": c.sha256,
        "byte_length": c.byte_length,
    }, content) for c, content in zip(record.crops, crops)))
    if framed != packet:
        raise ABContractError("frozen_packet_framing_mismatch")
    return packet, prompt, schema, crops


def build_paired_references(record: FrozenPacketRecord) -> tuple[PairedArmReference, ...]:
    common = {
        "packet_id": record.packet_id,
        "packet_sha256": record.packet_sha256,
        "prompt_sha256": record.prompt_sha256,
        "schema_sha256": record.schema_sha256,
        "ordered_crop_sha256s": tuple(item.sha256 for item in record.crops),
        "target_subtype": record.target_subtype,
        "generation_settings_fingerprint": record.generation_settings_fingerprint,
    }
    return (
        PairedArmReference(arm=ExperimentArm.A, model_id=ARM_A_MODEL_ID, **common),
        PairedArmReference(arm=ExperimentArm.B, model_id=ARM_B_MODEL_ID, **common),
    )


def calculate_paired_budget(
    *, expected_input_tokens_per_arm: int, expected_output_tokens_per_arm: int,
    phase_a_cumulative_spend_usd: Decimal, observed_packet_total_pixels: int = 1,
    observed_prompt_total_bytes: int = 1,
    safety_margin_multiplier: Decimal = Decimal("2"),
) -> PairedBudgetEstimate:
    if expected_input_tokens_per_arm <= 0 or expected_output_tokens_per_arm < 0:
        raise ABContractError("paired_usage_estimate_unreliable")
    if safety_margin_multiplier < 1:
        raise ABContractError("paired_safety_margin_invalid")
    if observed_packet_total_pixels <= 0 or observed_prompt_total_bytes <= 0:
        raise ABContractError("paired_observed_packet_metrics_invalid")
    money = Decimal("0.000001")
    budgets: list[ModelBudget] = []
    for profile in model_profiles():
        maximum_input = int(Decimal(expected_input_tokens_per_arm) * safety_margin_multiplier)
        maximum_output = ELIGIBLE_PACKET_COUNT * MAX_OUTPUT_TOKENS
        expected = (
            Decimal(expected_input_tokens_per_arm) * profile.input_price_usd_per_million
            + Decimal(expected_output_tokens_per_arm) * profile.output_price_usd_per_million
        ) / Decimal(1_000_000)
        maximum = (
            Decimal(maximum_input) * profile.input_price_usd_per_million
            + Decimal(maximum_output) * profile.output_price_usd_per_million
        ) / Decimal(1_000_000)
        budgets.append(ModelBudget(
            arm=profile.arm, model_id=profile.model_id,
            expected_input_tokens=expected_input_tokens_per_arm,
            expected_output_tokens=expected_output_tokens_per_arm,
            maximum_input_tokens=maximum_input, maximum_output_tokens=maximum_output,
            estimated_cost_usd=expected.quantize(money, rounding=ROUND_UP),
            maximum_reserved_usd=maximum.quantize(money, rounding=ROUND_UP),
            expected_cost_per_packet_usd=(expected / ELIGIBLE_PACKET_COUNT).quantize(money, rounding=ROUND_UP),
            maximum_reservation_per_packet_usd=(maximum / ELIGIBLE_PACKET_COUNT).quantize(money, rounding=ROUND_UP),
        ))
    expected_total = sum((item.estimated_cost_usd for item in budgets), Decimal("0"))
    maximum_total = sum((item.maximum_reserved_usd for item in budgets), Decimal("0"))
    return PairedBudgetEstimate(
        model_budgets=tuple(budgets), expected_total_usd=expected_total,
        maximum_reserved_total_usd=maximum_total,
        phase_a_cumulative_spend_usd=phase_a_cumulative_spend_usd,
        phase_a_remaining_before_usd=PHASE_A_CAP_USD - phase_a_cumulative_spend_usd,
        phase_a_remaining_after_maximum_usd=(
            PHASE_A_CAP_USD - phase_a_cumulative_spend_usd - maximum_total
        ),
        safety_margin_multiplier=safety_margin_multiplier,
        observed_packet_total_pixels=observed_packet_total_pixels,
        observed_prompt_total_bytes=observed_prompt_total_bytes,
    )


class OneShotPairedLedger:
    """Authorizes exactly one isolated request per (packet, arm)."""

    def __init__(self, manifest: FrozenPairedManifest) -> None:
        self._eligible = {item.packet_id for item in manifest.packet_records}
        self._reserved: set[tuple[str, ExperimentArm]] = set()
        self._consumed: set[tuple[str, ExperimentArm]] = set()
        self._outputs: dict[tuple[str, ExperimentArm], str] = {}

    def register(self, packet_id: str, arm: ExperimentArm, reservation: SpendReservation) -> None:
        key = (packet_id, arm)
        if packet_id not in self._eligible:
            raise ABContractError("paired_packet_not_eligible")
        if key in self._reserved or key in self._consumed:
            raise ABContractError("paired_packet_arm_request_limit_reached")
        if reservation.status != "reserved":
            raise ABContractError("paired_spend_required_before_dispatch")
        self._reserved.add(key)

    def consume(self, packet_id: str, arm: ExperimentArm) -> None:
        key = (packet_id, arm)
        if key not in self._reserved:
            raise ABContractError("paired_dispatch_without_reservation")
        if key in self._consumed:
            raise ABContractError("paired_packet_arm_request_limit_reached")
        self._consumed.add(key)

    def record_output(self, packet_id: str, arm: ExperimentArm, output_sha256: str) -> None:
        key = (packet_id, arm)
        if key not in self._consumed or key in self._outputs:
            raise ABContractError("paired_output_lifecycle_invalid")
        if len(output_sha256) != 64:
            raise ABContractError("paired_output_hash_invalid")
        self._outputs[key] = output_sha256

    def isolated_output(self, packet_id: str, arm: ExperimentArm) -> str | None:
        return self._outputs.get((packet_id, arm))


def reserve_packet_arm(
    *, controller: ExperimentSpendController, packet_id: str,
    arm: ExperimentArm, budget: PairedBudgetEstimate,
) -> SpendReservation:
    selected = next(item for item in budget.model_budgets if item.arm is arm)
    return controller.reserve(
        phase=ExperimentPhase.A,
        estimated_cost_usd=selected.maximum_reserved_usd / Decimal(ELIGIBLE_PACKET_COUNT),
        provider="gemini", model_id=selected.model_id,
        profile_id=f"phase-a-paired-supplementary-{arm.value}",
        stage="controlled_paired_supplementary_ab",
        purpose="paired_single_frozen_packet_one_shot",
        document_sha256="",
    )


def git_safe_summary(
    manifest: FrozenPairedManifest, budget: PairedBudgetEstimate,
) -> dict[str, Any]:
    return {
        "schema_version": AB_CONTRACT_VERSION,
        "arm_models": [ARM_A_MODEL_ID, ARM_B_MODEL_ID],
        "eligible_packet_count": len(manifest.packet_records),
        "excluded_localization_target_count": len(manifest.excluded_localization_targets),
        "byte_identical_pairing": True,
        "offline_regeneration_equal_count": sum(
            item.offline_regeneration_equal for item in manifest.packet_records
        ),
        "historical_results_are_formal_arm": False,
        "capability_probe_status": "operator_confirmation_required",
        "expected_cost_usd": str(budget.expected_total_usd),
        "maximum_reserved_usd": str(budget.maximum_reserved_total_usd),
        "ab_sub_budget_usd": str(budget.ab_sub_budget_usd),
        "external_provider_calls": 0,
    }


__all__ = [
    "AB_CONTRACT_VERSION", "ABContractError", "AB_SERIALIZATION_VERSION",
    "ARM_A_MODEL_ID", "ARM_B_MODEL_ID", "ExperimentArm", "PacketOutcome",
    "ModelProfile", "FrozenCropReference", "FrozenPacketRecord", "PairedArmReference",
    "ExcludedLocalizationTarget", "SyntheticCapabilityProbeContract", "FrozenPairedManifest",
    "PairedExecutionPolicy", "ModelBudget", "PairedBudgetEstimate", "PacketArmEvaluation",
    "PairedAggregateEvaluation",
    "OneShotPairedLedger", "build_paired_references", "calculate_paired_budget",
    "canonical_json_bytes", "canonical_json_sha256", "frame_packet_bytes", "git_safe_summary",
    "load_verified_packet_material", "model_profiles", "reserve_packet_arm", "sha256_bytes",
]
