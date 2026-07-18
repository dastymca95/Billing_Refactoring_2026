from pathlib import Path

from webapp.backend.services import vendor_detection


def test_registered_processor_identity_routes_without_legacy_keyword_entry(monkeypatch):
    monkeypatch.setattr(
        vendor_detection,
        "_registered_identity_specs",
        lambda: (("example_power", ("Example Regional Power System", "erp")),),
    )

    detected = vendor_detection.detect_vendor_from_text(
        Path("numeric-document.pdf"),
        "ACCOUNT 101\nEXAMPLE REGIONAL POWER SYSTEM\nAMOUNT DUE 42.10",
    )

    assert detected is not None
    assert detected["vendor_key"] == "example_power"
    assert detected["processing_mode"] == "manual"  # test identity is not registered at runtime
    assert detected["reason"] == "registered deterministic identity: example_power"


def test_short_operator_alias_cannot_authorize_deterministic_routing(monkeypatch):
    monkeypatch.setattr(
        vendor_detection,
        "_registered_identity_specs",
        lambda: (("example_power", ("erp",)),),
    )

    assert vendor_detection._detect_registered_processor_identity(
        "ERP appears incidentally in unrelated source text",
    ) is None


def test_ambiguous_equal_length_identity_does_not_route(monkeypatch):
    monkeypatch.setattr(
        vendor_detection,
        "_registered_identity_specs",
        lambda: (
            ("vendor_a", ("Shared Billing Identity",)),
            ("vendor_b", ("Shared Billing Identity",)),
        ),
    )

    assert vendor_detection._detect_registered_processor_identity(
        "Shared Billing Identity",
    ) is None


def test_longest_registered_identity_wins(monkeypatch):
    monkeypatch.setattr(
        vendor_detection,
        "_registered_identity_specs",
        lambda: (
            ("broad", ("Regional Power",)),
            ("specific", ("Example Regional Power",)),
        ),
    )

    detected = vendor_detection._detect_registered_processor_identity(
        "Invoice from Example Regional Power",
    )

    assert detected is not None
    assert detected["vendor_key"] == "specific"


def test_registered_shelbyville_identity_routes_to_existing_processor(monkeypatch):
    # Regression for the numeric-filename batch that previously escaped to AI.
    # The production implementation loads this identity from the registered
    # YAML contract; the test runtime intentionally uses sanitized assets.
    monkeypatch.setattr(
        vendor_detection,
        "_registered_identity_specs",
        lambda: (("shelbyville_power_system", ("Shelbyville Power System",)),),
    )
    detected = vendor_detection.detect_vendor_from_text(
        Path("2933816.pdf"),
        "SHELBYVILLE POWER SYSTEM\nTOTAL CURRENT CHARGES 122.21",
    )

    assert detected is not None
    assert detected["vendor_key"] == "shelbyville_power_system"
    assert detected["processing_mode"] == "deterministic"
