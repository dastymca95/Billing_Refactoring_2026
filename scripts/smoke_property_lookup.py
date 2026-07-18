"""Regression checks for the shared property and unit lookup."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.property_lookup import match_by_address, match_by_property_name  # noqa: E402


def test_property_abbreviation_fallback_and_composite_units() -> None:
    expected = {
        "115 WALNUT ST 13": "115-13",
        "115 WALNUT ST 2": "115-2",
        "115 WALNUT ST 8": "115-8",
        "115 WALNUT ST 9": "115-9",
    }
    for address, unit in expected.items():
        match = match_by_address(address)
        assert match is not None
        assert match.property_abbreviation == "TVUGDG"
        assert match.unit_number == unit

    common_area = match_by_address("115 WALNUT ST SL")
    assert common_area is not None
    assert common_area.property_abbreviation == "TVUGDG"
    assert common_area.unit_number == ""


def test_property_name_normalization() -> None:
    expected = {
        "Liberty Landings": "LLA",
        "Oak Tree Farms": "OTF",
        "The Oakley at Pro Park": "OG-PPA",
        "Rowe at Gate 1, The": "TRG1",
    }
    for property_name, abbreviation in expected.items():
        match = match_by_property_name(property_name)
        assert match is not None, property_name
        assert match.property_abbreviation == abbreviation, (property_name, match)


if __name__ == "__main__":
    test_property_abbreviation_fallback_and_composite_units()
    test_property_name_normalization()
    print("smoke_property_lookup: ok")
