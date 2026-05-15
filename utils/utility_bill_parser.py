"""Shared utility-bill helpers exposed for vendor processors.

The parser for each utility vendor still lives in that vendor's processor.
This module exposes the common normalization primitives those parsers should
use after extracting candidate bill fields.
"""

from webapp.backend.services.utility_processor_common import (  # noqa: F401
    UtilityChargeLine,
    build_utility_invoice_number,
    compose_invoice_description,
    compose_line_item_description,
    filter_exportable_utility_lines,
    load_vendor_config,
    validate_utility_template_rows,
)

