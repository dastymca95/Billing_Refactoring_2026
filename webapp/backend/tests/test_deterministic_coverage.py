from __future__ import annotations

from unittest.mock import patch

from webapp.backend.services import batch_processor
from webapp.backend.services import deterministic_coverage as coverage
from webapp.backend.services import vendor_rules


def test_inventory_comes_from_registered_processors_and_reports_health():
    items = coverage.inventory()
    assert len(items) == len(batch_processor._PROCESSOR_LOADERS)
    assert all(item.processor_available for item in items)
    assert all(item.processor_entrypoint for item in items)
    assert {item.vendor_key for item in items} == coverage.registered_vendor_keys()


def test_yaml_alone_never_creates_deterministic_coverage():
    coverage.invalidate_inventory_cache()
    with patch.object(coverage, "deterministic_processor_audit", return_value={"processors": []}):
        assert coverage.inventory() == []
    coverage.invalidate_inventory_cache()


def test_vendor_resolution_is_exact_and_never_fuzzy():
    exact = coverage.resolve_vendor("Hopkinsville Water Environment Authority")
    assert exact is not None
    assert exact.vendor_key == "hopkinsville_water_environment_authority"
    assert coverage.resolve_vendor("Hopkinsville Wat") is None
    assert coverage.resolve_vendor("unrelated vendor") is None


def test_code_managed_processor_is_not_presented_as_browser_editable():
    item = coverage.coverage_for_key("lowes")
    assert item is not None
    assert item.implementation_kind == "code_managed"
    assert item.editable is False
    assert item.config_present is False


def test_registered_declarative_patterns_are_editable_but_arbitrary_logic_is_not():
    item = coverage.coverage_for_key("alabama_power")
    assert item is not None and item.editable and item.patterns
    pattern = item.patterns[0]
    assert vendor_rules.validate_patch(item.vendor_key, {pattern.path: pattern.values}) == []

    issues = vendor_rules.validate_patch(item.vendor_key, {"python.logic": "return unsafe"})
    assert issues and "not editable" in issues[0]["message"]

    issues = vendor_rules.validate_patch(item.vendor_key, {"invented.patterns": ["unsafe"]})
    assert issues and "existing declarative" in issues[0]["message"]


def test_rules_studio_discovers_registered_configured_vendors_without_vendor_hardcoding():
    listed = vendor_rules.list_editable_vendors()
    listed_keys = {item["vendor_key"] for item in listed}
    assert "alabama_power" in listed_keys
    assert "hopkinsville_water_environment_authority" in listed_keys
    assert "richmond_utilities" in listed_keys
    assert "lowes" not in listed_keys
