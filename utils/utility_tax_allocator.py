"""Compatibility wrapper for shared utility tax allocation."""

from webapp.backend.services.utility_processor_common import (  # noqa: F401
    UtilityChargeLine,
    UtilityTaxAllocation,
    allocate_tax_proportionally,
    filter_exportable_utility_lines,
    money,
)

