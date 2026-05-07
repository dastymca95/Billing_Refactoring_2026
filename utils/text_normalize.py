"""Phase 2L — shared text-normalisation helpers."""

from __future__ import annotations

import re
from typing import Any


_WORD_CHAR_RE = re.compile(r"[A-Za-z]")


def to_sentence_case(value: Any) -> Any:
    """Project-wide description casing: first alphabetic character
    upper-cased, every other alphabetic character lower-cased.

    Punctuation, digits, and whitespace are preserved exactly. Numbers
    inside the string ("0035-27437-059") and dates ("12/04/25-01/06/26")
    pass through untouched. Only the *case* of letters changes.
    """
    if value in (None, ""):
        return value
    s = str(value)
    if not s:
        return s
    out = list(s.lower())
    for i, ch in enumerate(out):
        if _WORD_CHAR_RE.match(ch):
            out[i] = ch.upper()
            break
    return "".join(out)
