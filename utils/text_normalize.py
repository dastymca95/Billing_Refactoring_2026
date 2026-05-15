"""Backward-compatible text-normalization import surface."""

from __future__ import annotations

from utils.text_normalization import (  # noqa: F401
    normalize_service_address_for_description,
    normalize_source_line_description,
    proper_case_preserve_acronyms,
    to_sentence_case,
)
