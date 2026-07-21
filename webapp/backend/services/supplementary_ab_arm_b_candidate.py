"""Safe Arm B replacement overlay for the frozen supplementary A/B bundle.

The overlay never reads or rewrites the private bundle.  It records the model
transition, paid pricing, and budget envelope needed for a separately
authorized execution that must reuse the already frozen bytes verbatim.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_UP

from pydantic import BaseModel, ConfigDict, Field, model_validator


CONTRACT_VERSION = "phase-a-supplementary-arm-b-candidate/1.0"
PRICING_VERSION = "google-gemini-standard-2026-07-20"
AUTHORIZED_HOST = "generativelanguage.googleapis.com"
FROZEN_BUNDLE_REFERENCE = "paired-bundle-v1"
PACKET_COUNT = 5
MAX_OUTPUT_TOKENS_PER_PACKET = 2048
PHASE_A_CAP_USD = Decimal("10.00")
AB_SUB_CAP_USD = Decimal("1.00")

ARM_A_MODEL_ID = "gemini-3.1-flash-lite"
ARM_A_INPUT_PRICE_USD_PER_MILLION = Decimal("0.25")
ARM_A_OUTPUT_PRICE_USD_PER_MILLION = Decimal("1.50")

PREVIOUS_ARM_B_MODEL_ID = "gemini-3.5-flash"
PREVIOUS_ARM_B_STATUS = "temporarily_unavailable_after_bounded_retry"
ARM_B_CANDIDATE_MODEL_ID = "gemini-3-flash-preview"
ARM_B_INPUT_PRICE_USD_PER_MILLION = Decimal("0.50")
ARM_B_OUTPUT_PRICE_USD_PER_MILLION = Decimal("3.00")


class ArmCostEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: str
    model_id: str
    expected_input_tokens: int = Field(gt=0)
    expected_output_tokens: int = Field(ge=0)
    maximum_input_tokens: int = Field(gt=0)
    maximum_output_tokens: int = Field(gt=0)
    input_price_usd_per_million: Decimal
    output_price_usd_per_million: Decimal
    expected_cost_usd: Decimal
    maximum_reserved_usd: Decimal


class ArmBCandidateBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pricing_version: str = PRICING_VERSION
    pricing_basis: str = "official_google_standard_paid_pricing"
    estimate_basis: str = "frozen_five_packet_observed_token_usage"
    packet_count: int = PACKET_COUNT
    expected_input_tokens_per_arm: int
    expected_output_tokens_per_arm: int
    safety_margin_multiplier: Decimal
    arms: tuple[ArmCostEstimate, ArmCostEstimate]
    expected_total_usd: Decimal
    maximum_reserved_total_usd: Decimal
    ab_sub_cap_usd: Decimal = AB_SUB_CAP_USD
    phase_a_cumulative_spend_usd: Decimal
    phase_a_remaining_before_usd: Decimal
    phase_a_remaining_after_maximum_usd: Decimal

    @model_validator(mode="after")
    def validate_caps(self) -> "ArmBCandidateBudget":
        if self.maximum_reserved_total_usd > self.ab_sub_cap_usd:
            raise ValueError("paired_ab_sub_budget_exceeded")
        if self.phase_a_remaining_after_maximum_usd < 0:
            raise ValueError("phase_a_budget_exceeded")
        return self


class PreparedArmBExecutionOverlay(BaseModel):
    """Non-authorizing reference to the existing immutable paired bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = CONTRACT_VERSION
    bundle_reference: str = FROZEN_BUNDLE_REFERENCE
    bundle_opened_during_preparation: bool = False
    packet_count: int = PACKET_COUNT
    packet_bytes_must_remain_identical: bool = True
    crop_bytes_and_order_must_remain_identical: bool = True
    packet_prompt_schema_fingerprints_must_remain_identical: bool = True
    historical_results_must_remain_unchanged: bool = True
    previous_arm_b_model_id: str = PREVIOUS_ARM_B_MODEL_ID
    previous_arm_b_status: str = PREVIOUS_ARM_B_STATUS
    arm_a_model_id: str = ARM_A_MODEL_ID
    arm_b_candidate_model_id: str = ARM_B_CANDIDATE_MODEL_ID
    native_generate_content_only: bool = True
    authorized_host: str = AUTHORIZED_HOST
    technically_eligible: bool
    private_execution_authorized: bool = False
    separate_private_authorization_required: bool = True
    budget: ArmBCandidateBudget


def _arm_cost(
    *,
    arm: str,
    model_id: str,
    expected_input_tokens: int,
    expected_output_tokens: int,
    maximum_input_tokens: int,
    maximum_output_tokens: int,
    input_rate: Decimal,
    output_rate: Decimal,
) -> ArmCostEstimate:
    unit = Decimal("0.000001")
    million = Decimal(1_000_000)
    expected = (
        Decimal(expected_input_tokens) * input_rate
        + Decimal(expected_output_tokens) * output_rate
    ) / million
    maximum = (
        Decimal(maximum_input_tokens) * input_rate
        + Decimal(maximum_output_tokens) * output_rate
    ) / million
    return ArmCostEstimate(
        arm=arm,
        model_id=model_id,
        expected_input_tokens=expected_input_tokens,
        expected_output_tokens=expected_output_tokens,
        maximum_input_tokens=maximum_input_tokens,
        maximum_output_tokens=maximum_output_tokens,
        input_price_usd_per_million=input_rate,
        output_price_usd_per_million=output_rate,
        expected_cost_usd=expected.quantize(unit, rounding=ROUND_UP),
        maximum_reserved_usd=maximum.quantize(unit, rounding=ROUND_UP),
    )


def calculate_candidate_budget(
    *,
    expected_input_tokens_per_arm: int,
    expected_output_tokens_per_arm: int,
    phase_a_cumulative_spend_usd: Decimal,
    safety_margin_multiplier: Decimal = Decimal("2"),
) -> ArmBCandidateBudget:
    if expected_input_tokens_per_arm <= 0 or expected_output_tokens_per_arm < 0:
        raise ValueError("paired_usage_estimate_unreliable")
    if safety_margin_multiplier < 1:
        raise ValueError("paired_safety_margin_invalid")
    maximum_input = int(
        Decimal(expected_input_tokens_per_arm) * safety_margin_multiplier
    )
    maximum_output = PACKET_COUNT * MAX_OUTPUT_TOKENS_PER_PACKET
    arms = (
        _arm_cost(
            arm="arm_a",
            model_id=ARM_A_MODEL_ID,
            expected_input_tokens=expected_input_tokens_per_arm,
            expected_output_tokens=expected_output_tokens_per_arm,
            maximum_input_tokens=maximum_input,
            maximum_output_tokens=maximum_output,
            input_rate=ARM_A_INPUT_PRICE_USD_PER_MILLION,
            output_rate=ARM_A_OUTPUT_PRICE_USD_PER_MILLION,
        ),
        _arm_cost(
            arm="arm_b",
            model_id=ARM_B_CANDIDATE_MODEL_ID,
            expected_input_tokens=expected_input_tokens_per_arm,
            expected_output_tokens=expected_output_tokens_per_arm,
            maximum_input_tokens=maximum_input,
            maximum_output_tokens=maximum_output,
            input_rate=ARM_B_INPUT_PRICE_USD_PER_MILLION,
            output_rate=ARM_B_OUTPUT_PRICE_USD_PER_MILLION,
        ),
    )
    expected_total = sum((item.expected_cost_usd for item in arms), Decimal("0"))
    maximum_total = sum((item.maximum_reserved_usd for item in arms), Decimal("0"))
    return ArmBCandidateBudget(
        expected_input_tokens_per_arm=expected_input_tokens_per_arm,
        expected_output_tokens_per_arm=expected_output_tokens_per_arm,
        safety_margin_multiplier=safety_margin_multiplier,
        arms=arms,
        expected_total_usd=expected_total,
        maximum_reserved_total_usd=maximum_total,
        phase_a_cumulative_spend_usd=phase_a_cumulative_spend_usd,
        phase_a_remaining_before_usd=PHASE_A_CAP_USD - phase_a_cumulative_spend_usd,
        phase_a_remaining_after_maximum_usd=(
            PHASE_A_CAP_USD - phase_a_cumulative_spend_usd - maximum_total
        ),
    )


__all__ = [
    "ARM_B_CANDIDATE_MODEL_ID",
    "PREVIOUS_ARM_B_STATUS",
    "PreparedArmBExecutionOverlay",
    "calculate_candidate_budget",
]
