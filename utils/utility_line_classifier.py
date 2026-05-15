"""Compatibility wrapper for utility line classification and GL defaults."""

from webapp.backend.services.utility_processor_common import (  # noqa: F401
    classify_utility_line,
    classify_utility_line_detail,
    default_gl_for_line,
    is_non_expense_line,
    service_family,
)
