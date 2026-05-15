"""Shared text normalization for ResMan-facing output fields."""

from __future__ import annotations

import re
from typing import Any


ACRONYMS = {
    "ACH",
    "AI",
    "CDE",
    "CPWS",
    "EPB",
    "GL",
    "HWEA",
    "ID",
    "KUB",
    "OCR",
    "PDF",
    "PO",
    "RECC",
    "TK",
    "TVA",
    "URL",
}

TITLE_WORDS = {
    "apt": "Apt",
    "ave": "Ave",
    "avenue": "Ave",
    "blvd": "Blvd",
    "circle": "Cir",
    "cir": "Cir",
    "court": "Ct",
    "ct": "Ct",
    "dr": "Dr",
    "drive": "Dr",
    "hwy": "Hwy",
    "highway": "Hwy",
    "inc": "Inc",
    "lane": "Ln",
    "ln": "Ln",
    "llc": "LLC",
    "lp": "LP",
    "pkwy": "Pkwy",
    "rd": "Rd",
    "road": "Rd",
    "ste": "Ste",
    "street": "St",
    "st": "St",
    "suite": "Suite",
    "unit": "Unit",
    "way": "Way",
    "n": "N",
    "s": "S",
    "e": "E",
    "w": "W",
    "ne": "NE",
    "nw": "NW",
    "se": "SE",
    "sw": "SW",
}

SMALL_WORDS = {"and", "or", "of", "the", "to", "for", "by", "in", "on", "at"}

_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?|\d+[A-Za-z]*|\S")
_STATE_ZIP_RE = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", re.I)


def compact_spaces(value: Any) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split())


def proper_case_preserve_acronyms(value: Any) -> str:
    """Return Excel-PROPER-like text while preserving project acronyms."""

    text = compact_spaces(value)
    if not text:
        return ""
    pieces: list[str] = []
    for token in re.split(r"(\s+)", text):
        if not token or token.isspace():
            pieces.append(token)
            continue
        pieces.append(_proper_token(token))
    rendered = "".join(pieces)
    # Re-normalize common OCR/address spacing around punctuation.
    rendered = re.sub(r"\s+([,.;:])", r"\1", rendered)
    rendered = re.sub(r",(?=\S)", ", ", rendered)
    rendered = re.sub(r"([(])\s+", r"\1", rendered)
    return rendered.strip()


def normalize_service_address_for_description(value: Any) -> str:
    """Normalize an address for descriptions, omitting city/state/ZIP."""

    text = compact_spaces(value)
    if not text:
        return ""
    # Comma-separated addresses are easiest and safest: keep the street/unit.
    if "," in text:
        first = text.split(",", 1)[0]
        return proper_case_preserve_acronyms(first)

    # Remove trailing state/ZIP and the city token immediately before it when
    # the street suffix has already appeared. This avoids putting
    # "Hopkinsville, KY 42240" into ResMan descriptions.
    text = _STATE_ZIP_RE.sub("", text).strip(" ,")
    match = re.search(
        r"^(?P<street>.*?\b(?:St|Street|Rd|Road|Ave|Avenue|Dr|Drive|Ln|Lane|Ct|Court|Blvd|Pkwy|Way|Hwy|Highway|Cir|Circle)\b"
        r"(?:\s+(?:Apt|Unit|Ste|Suite)\s*[A-Za-z0-9-]+)?)\s+"
        r"(?P<city>[A-Za-z][A-Za-z .'-]{2,})$",
        text,
        flags=re.I,
    )
    if match:
        text = match.group("street")
    return proper_case_preserve_acronyms(text)


def normalize_source_line_description(value: Any) -> str:
    return proper_case_preserve_acronyms(value)


def looks_like_city_state_zip(value: Any) -> bool:
    return bool(_STATE_ZIP_RE.search(str(value or "")))


def to_sentence_case(value: Any) -> Any:
    """Legacy compatibility wrapper.

    New output contract wants Proper Case, not old sentence case. The public
    function name stays for older imports, but callers get the new behavior.
    """

    if value in (None, ""):
        return value
    return proper_case_preserve_acronyms(value)


def _proper_token(token: str) -> str:
    # Keep URLs/emails and obvious IDs intact.
    if "://" in token or "@" in token:
        return token
    if re.search(r"\d", token) and re.search(r"[A-Za-z]", token):
        return _proper_alphanumeric(token)

    leading = re.match(r"^\W+", token)
    trailing = re.search(r"\W+$", token)
    prefix = leading.group(0) if leading else ""
    suffix = trailing.group(0) if trailing else ""
    core = token[len(prefix): len(token) - len(suffix) if suffix else len(token)]
    if not core:
        return token
    if "-" in core:
        return prefix + "-".join(_proper_word(part, lower_small_words=False) for part in core.split("-")) + suffix
    return prefix + _proper_word(core) + suffix


def _proper_word(word: str, *, lower_small_words: bool = True) -> str:
    if not word:
        return word
    upper = word.upper()
    lower = word.lower()
    if upper in ACRONYMS:
        return upper
    if lower in TITLE_WORDS:
        return TITLE_WORDS[lower]
    if lower_small_words and lower in SMALL_WORDS:
        return lower
    return word[:1].upper() + word[1:].lower()


def _proper_alphanumeric(token: str) -> str:
    # Apartment/account-like tokens should remain stable, but normalize common
    # street unit suffixes such as "apt6" only when they are clear words.
    parts = re.split(r"([/-])", token)
    out: list[str] = []
    for part in parts:
        if part in {"/", "-"}:
            out.append(part)
        elif part.isalpha():
            out.append(_proper_word(part, lower_small_words=False))
        else:
            out.append(part)
    return "".join(out)


__all__ = [
    "ACRONYMS",
    "compact_spaces",
    "looks_like_city_state_zip",
    "normalize_service_address_for_description",
    "normalize_source_line_description",
    "proper_case_preserve_acronyms",
    "to_sentence_case",
]
