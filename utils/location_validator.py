"""
Location + Property Abbreviation validation against trusted reference data.

Used by every vendor processor to enforce the project-wide rule:

  * `Location` must come ONLY from trusted unit data
    (`Properties/Unit Info Clean.csv` is the primary source).
  * Blank `Location` is acceptable — better empty than wrong.
  * `Property Abbreviation` is mandatory for every ResMan row;
    missing values are flagged for hard manual review.
  * Garbage / OCR fragments (`NA`, `A`, `ATA`, `?`, `-`, etc.) must never
    survive into the export.

This module is YAML-driven; vendor processors pass in the
`location_validation_rules` block from their YAML.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Default reject list when YAML doesn't override.
_DEFAULT_REJECT = {
    "NA", "N/A", "NONE", "TBD", "?", "-", ".", "", "A", "ATA",
}

# Regex for "looks like a full street address": digit-prefix + street suffix.
_LOOKS_LIKE_FULL_ADDRESS_RE = re.compile(
    r"\b\d+[^,\n]*?\b(?:st|street|dr|drive|rd|road|ave|avenue|ln|lane|"
    r"blvd|boulevard|ct|court|cir|circle|pl|place|way|pkwy|hwy)\b",
    re.IGNORECASE,
)


@dataclass
class ValidationResult:
    """Outcome of validating a single (location, property_abbreviation)
    pair. The vendor processor mutates the invoice with `cleared_location`
    when invalid and appends `manual_review_reasons` to the invoice's list."""
    cleared_location: bool = False
    location_value: str = ""
    property_abbreviation: str = ""
    manual_review_reasons: list[str] = field(default_factory=list)


def _normalize_token(s: str) -> str:
    return (s or "").strip().upper()


@dataclass
class TrustedUnitIndex:
    """Lazy index of trusted (property_abbreviation, unit_number) pairs from
    Unit Info Clean.csv (and optional fallback CSVs). Vendor processors
    create this once per run."""
    pairs: set[tuple[str, str]] = field(default_factory=set)
    units_by_property: dict[str, set[str]] = field(default_factory=dict)
    properties: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, sources: Iterable[Path], logger: Optional[logging.Logger] = None
             ) -> "TrustedUnitIndex":
        idx = cls()
        log = logger or logging.getLogger("location_validator")
        for path in sources:
            p = Path(path)
            if not p.is_file():
                continue
            try:
                raw = p.read_bytes()
                # Cope with cp1252 / latin-1 leaks the same way other helpers do.
                lines = None
                for enc in ("utf-8-sig", "cp1252", "latin-1"):
                    try:
                        lines = raw.decode(enc).splitlines()
                        break
                    except UnicodeDecodeError:
                        continue
                if lines is None:
                    continue
                reader = csv.DictReader(lines)
                for row in reader:
                    prop = (row.get("property_abbreviation")
                            or row.get("Property Abbreviation") or "").strip()
                    unit = (row.get("unit_number")
                            or row.get("Unit Number") or "").strip()
                    if prop:
                        idx.properties.add(_normalize_token(prop))
                    if prop and unit:
                        idx.pairs.add((_normalize_token(prop), _normalize_token(unit)))
                        idx.units_by_property.setdefault(
                            _normalize_token(prop), set()).add(_normalize_token(unit))
            except Exception as e:  # pragma: no cover
                log.warning("Could not load trusted units from %s: %s", p, e)
        return idx

    def has_unit_for_property(self, prop_abbr: str, unit: str) -> bool:
        return (_normalize_token(prop_abbr), _normalize_token(unit)) in self.pairs

    def has_unit_anywhere(self, unit: str) -> bool:
        u = _normalize_token(unit)
        return any(u in s for s in self.units_by_property.values())


def validate_location(
    *,
    location_value: str,
    property_abbreviation: str,
    trusted_index: TrustedUnitIndex,
    rules: dict,
) -> ValidationResult:
    """Validate a single Location value against trusted reference data.

    Returns a `ValidationResult` whose flags the caller acts on:
      * If `cleared_location` is True, replace the row's Location with "".
      * Append every entry in `manual_review_reasons` to the invoice's
        manual-review list.

    The function is conservative — when in doubt, clear and flag.
    """
    out = ValidationResult(
        location_value=location_value or "",
        property_abbreviation=property_abbreviation or "",
    )
    raw = (location_value or "").strip()
    if not raw:
        # Blank Location is allowed.
        return out

    if not (rules or {}).get("enabled", True):
        return out

    reject_strs = {_normalize_token(s)
                   for s in (rules.get("reject_values") or _DEFAULT_REJECT)}
    if _normalize_token(raw) in reject_strs:
        out.cleared_location = True
        out.location_value = ""
        out.manual_review_reasons.append(
            rules.get("manual_review_reason_invalid",
                       "invalid_location_not_in_unit_info_clean"),
        )
        return out

    if rules.get("reject_if_equals_property_abbreviation", True):
        if _normalize_token(raw) == _normalize_token(property_abbreviation) \
                and property_abbreviation:
            out.cleared_location = True
            out.location_value = ""
            out.manual_review_reasons.append(
                rules.get("manual_review_reason_invalid",
                           "invalid_location_not_in_unit_info_clean"),
            )
            return out

    if rules.get("reject_if_contains_full_street_address", True):
        if _LOOKS_LIKE_FULL_ADDRESS_RE.search(raw):
            out.cleared_location = True
            out.location_value = ""
            out.manual_review_reasons.append(
                rules.get("manual_review_reason_invalid",
                           "invalid_location_not_in_unit_info_clean"),
            )
            return out

    # Trusted-source check. If we have a property abbreviation, the
    # (property, unit) pair must exist in the trusted index. If we don't
    # have the property abbreviation yet, accept any unit that exists
    # somewhere in the trusted data.
    if property_abbreviation:
        if not trusted_index.has_unit_for_property(property_abbreviation, raw):
            out.cleared_location = True
            out.location_value = ""
            out.manual_review_reasons.append(
                rules.get("manual_review_reason_invalid",
                           "invalid_location_not_in_unit_info_clean"),
            )
            return out
    else:
        # No property — be even more conservative.
        if not trusted_index.has_unit_anywhere(raw):
            out.cleared_location = True
            out.location_value = ""
            out.manual_review_reasons.append(
                rules.get("manual_review_reason_invalid",
                           "invalid_location_not_in_unit_info_clean"),
            )
            return out

    return out


def validate_property_abbreviation(
    *,
    property_abbreviation: str,
    trusted_index: TrustedUnitIndex,
    rules: dict,
) -> list[str]:
    """Confirm the property abbreviation exists in the trusted data. Returns
    a list of manual-review reasons (empty if OK). Blank is NOT acceptable
    when this rule is enabled — Property Abbreviation is mandatory."""
    if not (rules or {}).get("enabled", True):
        return []
    pa = (property_abbreviation or "").strip()
    if not pa:
        return [rules.get("manual_review_reason_missing",
                          "property_abbreviation_missing")]
    if pa.upper() not in trusted_index.properties and trusted_index.properties:
        return [rules.get("manual_review_reason_missing",
                          "property_abbreviation_missing")]
    return []
