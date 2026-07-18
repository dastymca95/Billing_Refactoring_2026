import pytest

from webapp.backend.services.processing_route_gate import (
    EffectiveProcessingRoute,
    decide_processing_route,
)
from webapp.backend.services.processing_route_policy import (
    ProcessingRouteMode,
    ProcessingRouteResolution,
)


def resolution(mode: ProcessingRouteMode) -> ProcessingRouteResolution:
    return ProcessingRouteResolution(
        batch_id="batch_20260717_120000_001",
        filename="invoice.pdf",
        requested_mode=mode,
        inherited_from="document",
    )


@pytest.mark.parametrize(
    ("mode", "available", "route", "ai_authorized", "reason"),
    [
        (
            ProcessingRouteMode.AUTO_COST_SAFE,
            True,
            EffectiveProcessingRoute.DETERMINISTIC,
            False,
            "cost_safe_deterministic_default",
        ),
        (
            ProcessingRouteMode.AUTO_COST_SAFE,
            False,
            EffectiveProcessingRoute.AI,
            True,
            "cost_safe_ai_no_deterministic_processor",
        ),
        (
            ProcessingRouteMode.DETERMINISTIC_ONLY,
            True,
            EffectiveProcessingRoute.DETERMINISTIC,
            False,
            "operator_locked_deterministic",
        ),
        (
            ProcessingRouteMode.DETERMINISTIC_ONLY,
            False,
            EffectiveProcessingRoute.BLOCKED,
            False,
            "deterministic_processor_unavailable",
        ),
        (
            ProcessingRouteMode.AI_FALLBACK_ALLOWED,
            True,
            EffectiveProcessingRoute.DETERMINISTIC,
            True,
            "deterministic_first_ai_fallback_authorized",
        ),
        (
            ProcessingRouteMode.AI_FALLBACK_ALLOWED,
            False,
            EffectiveProcessingRoute.AI,
            True,
            "ai_authorized_no_deterministic_processor",
        ),
    ],
)
def test_route_matrix(mode, available, route, ai_authorized, reason):
    decision = decide_processing_route(
        resolution(mode),
        vendor_key="registered_vendor" if available else "unknown",
        deterministic_available=available,
        processor_id="processor.entrypoint" if available else None,
    )

    assert decision.effective_route == route
    assert decision.ai_fallback_authorized is ai_authorized
    assert decision.reason_code == reason
    assert decision.processor_id == ("processor.entrypoint" if available else None)


def test_availability_requires_a_nonempty_vendor_identity():
    decision = decide_processing_route(
        resolution(ProcessingRouteMode.DETERMINISTIC_ONLY),
        vendor_key=None,
        deterministic_available=True,
        processor_id="should-not-leak",
    )

    assert decision.effective_route == EffectiveProcessingRoute.BLOCKED
    assert decision.deterministic_available is False
    assert decision.ai_fallback_authorized is False
    assert decision.processor_id is None
