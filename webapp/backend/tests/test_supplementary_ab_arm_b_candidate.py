from decimal import Decimal

from webapp.backend.services.supplementary_ab_arm_b_candidate import (
    PREVIOUS_ARM_B_STATUS,
    PreparedArmBExecutionOverlay,
    calculate_candidate_budget,
)


def test_candidate_budget_uses_observed_frozen_usage_and_stays_below_subcap():
    budget = calculate_candidate_budget(
        expected_input_tokens_per_arm=27_507,
        expected_output_tokens_per_arm=3_719,
        phase_a_cumulative_spend_usd=Decimal("0.326568"),
    )

    assert budget.expected_total_usd == Decimal("0.037367")
    assert budget.maximum_reserved_total_usd == Decimal("0.087341")
    assert budget.maximum_reserved_total_usd < Decimal("1")
    assert budget.phase_a_remaining_after_maximum_usd == Decimal("9.586091")


def test_prepared_overlay_does_not_authorize_or_rewrite_frozen_bundle():
    budget = calculate_candidate_budget(
        expected_input_tokens_per_arm=27_507,
        expected_output_tokens_per_arm=3_719,
        phase_a_cumulative_spend_usd=Decimal("0.326568"),
    )
    overlay = PreparedArmBExecutionOverlay(
        technically_eligible=True,
        budget=budget,
    )

    assert overlay.previous_arm_b_status == PREVIOUS_ARM_B_STATUS
    assert overlay.arm_b_candidate_model_id == "gemini-3-flash-preview"
    assert overlay.bundle_opened_during_preparation is False
    assert overlay.packet_bytes_must_remain_identical is True
    assert overlay.private_execution_authorized is False
    assert overlay.separate_private_authorization_required is True
