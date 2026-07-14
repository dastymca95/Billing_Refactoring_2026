from webapp.backend.services.model_registry import (
    CapabilityDiscovery, ModelRegistry, ModelRole, ModelSpec,
)
from webapp.backend.services.reasoning_router import Route, RoutingSignals, RoutingStateMachine


def _registry():
    return ModelRegistry([
        ModelSpec("fast", "mock", frozenset({ModelRole.EXTRACTION_TEXT})),
        ModelSpec("vision", "mock", frozenset({ModelRole.EXTRACTION_VISION}), supports_vision=True),
        ModelSpec("gpt-5.6", "openai", frozenset({ModelRole.ACCOUNTING_REASONING}), strong_reasoner=True),
    ])


def test_configured_strong_model_is_not_available_without_discovery():
    discovery = CapabilityDiscovery(_registry(), advertised=[])
    assert discovery.discover("gpt-5.6").available is False
    assert discovery.strong_accounting_model() is None


def test_discovered_strong_model_can_only_route_to_shadow():
    discovery = CapabilityDiscovery(_registry(), advertised=["gpt-5.6"])
    router = RoutingStateMachine(discovery, text_available=True, vision_available=True)
    decision = router.decide_accounting_shadow(RoutingSignals(False, True, accounting_ambiguity=True))
    assert decision.route is Route.STRONG_REASONING_SHADOW
    assert decision.shadow_only is True
    assert decision.model_id == "gpt-5.6"


def test_extraction_routing_is_deterministic_and_capability_aware():
    router = RoutingStateMachine(CapabilityDiscovery(_registry(), []), text_available=True, vision_available=True)
    assert router.decide_extraction(RoutingSignals(True, True)).route is Route.DETERMINISTIC
    assert router.decide_extraction(RoutingSignals(False, False, image_document=True)).route is Route.AI_VISION
    assert router.decide_extraction(RoutingSignals(False, False)).route is Route.AI_TEXT


def test_no_capability_routes_to_manual_review():
    router = RoutingStateMachine(CapabilityDiscovery(_registry(), []), text_available=False, vision_available=False)
    assert router.decide_extraction(RoutingSignals(False, False)).route is Route.MANUAL_REVIEW
